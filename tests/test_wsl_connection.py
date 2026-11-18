"""WSL connection tests use temporary files and an emulated CLI, never real WSL."""
import hashlib
import json
import ast
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.native_state import WSL_HOST_ID
from packet_inspector.session_summary import SessionSummaries
from packet_inspector.storage import Store
from packet_inspector.token_window import TokenWindows
from packet_inspector.wsl_connection import (LABEL_SCRIPT, LABEL_QUERY_LAUNCH, PROBE_SCRIPT, LocalWsl, WslConnection,
                                             WslError, decode_output, validate_target)


THREAD = "01a190d5-28f6-7163-8cc0-8f1ac253d0f7"
PARENT = "01a190b5-1d4a-7193-9454-a66377d0af1f"
UPSTREAM = "https://example.test/codex"
ENDPOINT = "http://127.0.0.1:47843/relay/test-only"


class FakeWsl:
    def __init__(self, root):
        self.root = root
        self.distributions = ["Ubuntu-Test"]
        self.probe_error = None
        self.queries = 0
        self.config_path = root / "linux" / "config.toml"
        self.config_path.parent.mkdir()
        self.config_path.write_text('model="example"\n[mcp_servers.linux_tool]\ncommand="/usr/bin/tool"\n', encoding="utf8")

    def running(self):
        return self.distributions

    def home(self, distribution):
        return "/home/test/.codex"

    def config(self, distribution, home):
        return self.config_path if self.config_path.is_file() else None

    def probe(self, distribution, port, marker):
        if self.probe_error:
            raise WslError(self.probe_error)

    def labels(self, distribution, home, ids):
        self.queries += 1
        config_home = str(self.config(distribution, home).parent)
        result = subprocess.run([sys.executable, "-c", LABEL_SCRIPT, config_home], capture_output=True,
            input=json.dumps({"home": config_home, "ids": ids}).encode(), timeout=5)
        if result.returncode:
            raise AssertionError(result.stderr.decode())
        return json.loads(result.stdout.decode().split("CPI_LABELS=", 1)[1])

    def migrate_providers(self, distribution, home, aliases):
        from packet_inspector.provider_migration_worker import migrate
        return migrate(self.config_path.parent, self.root / "migration-backups", aliases, "codex_packet_inspector")


class WslConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeWsl(self.root)
        self.wsl = WslConnection(self.root / "data", self.backend)
        self.routes = {"windows": {"identity": "windows\nopenai", "provider": "openai", "upstream": UPSTREAM}}

    def apply(self, connected=True):
        def publish(routes):
            self.routes = dict(routes)
            self.published = dict(routes)
        return self.wsl.apply(connected, ENDPOINT, UPSTREAM, self.routes, publish)

    def test_route_publication_does_not_clear_a_live_request_lookup(self):
        live_routes = self.routes
        before = dict(live_routes)
        self.apply()
        self.assertEqual(live_routes, before)
        self.assertGreater(len(self.routes), len(live_routes))

    def test_metadata_only_wsl_migration_reuses_connection_restart_feedback(self):
        self.apply()
        config_before = self.backend.config_path.read_bytes()
        database = self.backend.config_path.parent / "state_99.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,model_provider TEXT)")
            db.execute("INSERT INTO threads VALUES ('linux-chat','openai')")
        result = self.apply()
        self.assertTrue(result["changed"])
        self.assertTrue(result["reload_required"])
        self.assertEqual(result["targets"][0]["migrated_sessions"], 1)
        self.assertEqual(self.backend.config_path.read_bytes(), config_before)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT model_provider FROM threads").fetchone()[0], "codex_packet_inspector")
        self.assertFalse(self.apply()["reload_required"])

    def test_linux_migration_runs_the_bundled_worker_in_isolated_native_python(self):
        result = {"changed": 2, "backups": ["/home/test/.codex/provider-migration-backups/test.json"],
                  "warnings": [], "databases": 1}
        backend = LocalWsl()
        with patch.object(backend, "run", return_value="screen size warning\nCPI_PROVIDER_MIGRATION=" + json.dumps(result)) as run:
            self.assertEqual(backend.migrate_providers("Ubuntu-Test", "/home/test/.codex", ["openai"]), result)
        args, keywords = run.call_args
        self.assertTrue(any('exec python3 -I -S -' in value for value in args[0]))
        self.assertEqual(args[0][-1], "/home/test/.codex")
        tree = ast.parse(keywords["data"].decode("utf8"))
        request = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "request" for target in node.targets))
        self.assertEqual(ast.literal_eval(request.value)["aliases"], ["openai"])

    def test_disconnected_metadata_check_does_not_reconnect_probe_or_start_wsl(self):
        self.apply()
        self.apply(False)
        before = self.backend.config_path.read_bytes()
        database = self.backend.config_path.parent / "state_99.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,model_provider TEXT)")
            db.execute("INSERT INTO threads VALUES ('linux-chat','openai')")
        with patch.object(self.backend, "probe") as probe, patch.object(self.backend, "home") as home:
            result = self.wsl.reconcile_sessions(ENDPOINT, UPSTREAM, self.routes)
        probe.assert_not_called()
        home.assert_not_called()
        self.assertEqual(result["targets"][0]["migrated_sessions"], 1)
        self.assertFalse(result["targets"][0]["active"])
        self.assertEqual(self.backend.config_path.read_bytes(), before)
        self.backend.distributions = []
        with patch.object(self.backend, "migrate_providers") as migration:
            result = self.wsl.reconcile_sessions(ENDPOINT, UPSTREAM, self.routes)
        migration.assert_not_called()
        self.assertEqual(result["targets"], [])

    def index(self, *, source='"cli"', name="聊天对话串"):
        with closing(sqlite3.connect(self.backend.config_path.parent / "state_42.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT,name TEXT,agent_nickname TEXT,source TEXT,updated_at_ms INTEGER)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (THREAD, "initial title", name, "Agent", source, 1234000))

    def wait_for(self, lookup):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = lookup()
            if result:
                return result
            time.sleep(.02)
        self.fail("Timed out waiting for the background metadata lookup")

    def candidates(self, wsl=None, *, key=None):
        reader = (wsl or self.wsl).labels
        return self.wait_for(lambda: reader({THREAD: (key, None)}))[THREAD]

    def test_connect_backup_routes_noop_restore_and_linux_tools_preserved(self):
        before = self.backend.config_path.read_bytes()
        result = self.apply()
        self.assertTrue(result["reload_required"])
        self.assertEqual(len(result["targets"]), 1)
        self.assertFalse(result["warnings"])
        data = tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))
        self.assertEqual(data["mcp_servers"]["linux_tool"]["command"], "/usr/bin/tool")
        self.assertFalse(data["features"]["enable_request_compression"])
        self.assertTrue(data["openai_base_url"].startswith(ENDPOINT + "/p/"))
        self.assertIn("windows", self.published)
        self.assertGreater(len(self.published), 1)
        route_id = data["openai_base_url"].rsplit("/", 1)[1]
        key = self.routes[route_id].pop("wsl_connection_key")
        self.assertEqual(key, next(self.wsl._targets())[0].name)
        self.assertNotIn("wsl_connection_key", self.routes["windows"])
        backups = list(self.backend.config_path.parent.glob("*.inspector-backup-*"))
        self.assertTrue(any(path.read_bytes() == before for path in backups))
        self.assertFalse(self.apply()["reload_required"])
        self.assertEqual(self.routes[route_id]["wsl_connection_key"], key)
        self.assertEqual(list(self.backend.config_path.parent.glob("*.inspector-backup-*")), backups)
        self.assertTrue(self.apply(False)["reload_required"])
        restored = tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))
        self.assertTrue(restored["features"]["enable_request_compression"])
        self.assertFalse(str(restored.get("openai_base_url", "")).startswith(ENDPOINT))
        self.assertEqual(restored["mcp_servers"], data["mcp_servers"])
        self.assertFalse(self.apply(False)["reload_required"])

    def test_failed_reachability_keeps_config_and_routes_unchanged(self):
        before, routes = self.backend.config_path.read_bytes(), dict(self.routes)
        self.backend.probe_error = "relay_unreachable"
        result = self.apply()
        self.assertFalse(result["targets"])
        self.assertEqual(result["warnings"][0]["reason"], "relay_unreachable")
        self.assertEqual(self.backend.config_path.read_bytes(), before)
        self.assertEqual(self.routes, routes)
        self.assertEqual(len(list(self.wsl._targets())), 1, "Read-only metadata discovery survives a failed probe")
        self.assertFalse(list(self.wsl.directory.glob("*/link.json")), "There is no managed Linux configuration yet")

    def test_missing_config_and_stopped_distros_are_not_created_or_started(self):
        self.backend.config_path.unlink()
        self.assertFalse(self.apply()["targets"])
        self.assertFalse(self.backend.config_path.exists())
        self.backend.distributions = []
        self.assertFalse(self.apply()["targets"])

    def test_restore_keeps_manual_edits_and_defers_stopped_targets(self):
        self.apply()
        self.backend.distributions = []
        current = self.backend.config_path.read_bytes()
        self.assertEqual(self.apply(False)["warnings"][0]["reason"], "restore_pending")
        self.assertEqual(self.backend.config_path.read_bytes(), current)
        self.backend.distributions = ["Ubuntu-Test"]
        text = current.decode().replace('model="example"', 'model="manual"')
        self.backend.config_path.write_text(text, encoding="utf8")
        self.apply(False)
        self.assertEqual(tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))["model"], "manual")

    def test_first_disconnect_restores_manually_connected_config_without_a_probe(self):
        self.backend.config_path.write_text('model="example"\nopenai_base_url="' + ENDPOINT + '/p/windows"\n'
            '[mcp_servers.linux_tool]\ncommand="/usr/bin/tool"\n', encoding="utf8")
        self.assertFalse(list(self.wsl._targets()))
        with patch.object(self.backend, "probe", side_effect=AssertionError("Disconnect must not depend on network probing")):
            result = self.apply(False)
        self.assertTrue(result["reload_required"])
        self.assertFalse(result["warnings"])
        data = tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))
        self.assertFalse(data.get("openai_base_url", "").startswith(ENDPOINT))
        self.assertTrue(data["features"]["enable_request_compression"])
        self.assertEqual(data["mcp_servers"]["linux_tool"]["command"], "/usr/bin/tool")

    def test_first_disconnect_does_not_change_a_direct_linux_configuration(self):
        before = self.backend.config_path.read_bytes()
        result = self.apply(False)
        self.assertFalse(result["targets"])
        self.assertEqual(self.backend.config_path.read_bytes(), before)

    def test_history_names_work_without_successful_connection_or_linux_python(self):
        path = self.backend.config_path.parent / "session_index.jsonl"
        path.write_text(json.dumps({"id": THREAD, "thread_name": "聊天对话串"}) + "\n", encoding="utf8")
        self.backend.probe_error = "relay_unreachable"
        self.apply()
        self.assertFalse(list(self.wsl.directory.glob("*/link.json")))
        fresh = WslConnection(self.root / "data", self.backend)
        with patch.object(self.backend, "labels", side_effect=WslError("python_not_found", exit_code=127)) as query:
            self.assertEqual(self.candidates(fresh)[0]["title"], "聊天对话串")
            self.assertEqual(self.candidates(fresh)[0]["title"], "聊天对话串")
            self.assertEqual(query.call_count, 1)
        details = json.loads((fresh.directory / "metadata-status.json").read_text(encoding="utf8"))
        self.assertEqual(details["errors"][0]["exit_code"], 127)

    def test_manual_history_discovery_works_before_any_connection_action(self):
        self.index()
        self.assertFalse(list(self.wsl._targets()))
        self.assertEqual(self.candidates()[0]["title"], "聊天对话串")
        self.assertFalse(list(self.wsl.directory.glob("*/link.json")))

    def test_subprocess_failure_retains_bounded_redacted_details(self):
        cli = LocalWsl()
        cli.executable = "test-wsl.exe"
        error = b'python3: command not found\nhttp://localhost/relay/demo-secret/p/id Bearer example.secret\n' + b'x' * 1200
        with patch("packet_inspector.wsl_connection.subprocess.run", return_value=subprocess.CompletedProcess([], 127, b'', error)):
            with self.assertRaises(WslError) as caught:
                cli.run(["--list"])
        self.assertEqual(caught.exception.exit_code, 127)
        self.assertIn("command not found", caught.exception.detail)
        self.assertNotIn("demo-secret", caught.exception.detail)
        self.assertNotIn("example.secret", caught.exception.detail)
        self.assertLessEqual(len(caught.exception.detail), 1000)

    def test_linux_metadata_script_runs_from_stdin_and_accepts_profile_output(self):
        self.index()
        ast.parse(LABEL_SCRIPT, feature_version=(3, 7))
        cli = LocalWsl()
        active_home = str(self.backend.config_path.parent)
        def run(arguments, *, data, **kwargs):
            self.assertEqual(arguments[3:7], ["sh", "-lc", LABEL_QUERY_LAUNCH, "codex-inspector-labels"])
            self.assertEqual(arguments[-1], str(self.backend.config_path.parent))
            result = subprocess.run([sys.executable, "-I", "-S", "-", active_home], input=data,
                                    cwd=arguments[-1], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return "profile notice\n" + result.stdout.decode()
        with patch.object(cli, "run", side_effect=run):
            result = cli.labels("Ubuntu-Test", str(self.backend.config_path.parent), [THREAD])
            active_home = str(self.root / "another-home")
            with self.assertRaisesRegex(WslError, "codex_home_changed"):
                cli.labels("Ubuntu-Test", str(self.backend.config_path.parent), [THREAD])
        self.assertEqual(result["rows"][0][0], THREAD)

    def test_metadata_ignores_broken_import_paths_and_runs_in_the_linux_data_directory(self):
        self.index()
        received = self.root / "ReceiveFiles"
        received.mkdir()
        (received / "json.py").write_text(
            "raise OSError(5, 'Input/output error', '/mnt/d/eSpace_Desktop/ReceiveFiles')\n", encoding="utf8")
        polluted = {**os.environ, "PYTHONPATH": str(received)}
        # Reproduce the colleague's failure before testing the isolated launch.
        before = subprocess.run([sys.executable, "-S", "-c", "import json"], cwd=received,
                                env=polluted, capture_output=True, timeout=5)
        self.assertNotEqual(before.returncode, 0)
        self.assertIn(b"Input/output error", before.stderr)
        bash = Path(r"C:/Program Files/Git/bin/bash.exe") if sys.platform == "win32" else Path("/bin/bash")
        if not bash.is_file():
            self.skipTest("Bash is required to exercise the real shell launch")
        home = self.backend.config_path.parent.as_posix()
        polluted["CODEX_HOME"] = home
        cli = LocalWsl()
        def run(arguments, *, data, **kwargs):
            # Keep the production shell command/flags/arguments. Substitute only
            # the Python executable, because this test does not install WSL.
            command = arguments[5].replace("exec python3", 'exec "' + Path(sys.executable).as_posix() + '"')
            self.assertEqual(arguments[-1], home)
            script = b"import os,sys\nassert sys.flags.isolated and sys.flags.no_site\n" + data
            script += ("\nprint('CPI_CWD='+os.getcwd())\n").encode()
            after = subprocess.run([str(bash), "-lc", command, arguments[6], arguments[7]], input=script,
                                   env=polluted, cwd=received, capture_output=True, timeout=10)
            self.assertEqual(after.returncode, 0, after.stderr.decode(errors="replace"))
            lines = after.stdout.decode().splitlines()
            native_cwd = next(line.removeprefix("CPI_CWD=") for line in lines if line.startswith("CPI_CWD="))
            self.assertEqual(Path(native_cwd).resolve(), self.backend.config_path.parent.resolve())
            return after.stdout.decode()
        with patch.object(cli, "run", side_effect=run):
            result = cli.labels("Ubuntu-Test", home, [THREAD])
        self.assertEqual(result["rows"][0][0], THREAD)

    def test_linux_metadata_is_queried_natively_cached_and_used_for_grouping(self):
        self.apply()
        self.index()
        db_path = self.backend.config_path.parent / "state_42.sqlite"
        before = db_path.read_bytes()
        summaries = SessionSummaries(TokenWindows(self.root / "windows"), wsl_labels=self.wsl.labels)
        labels = self.wait_for(lambda: summaries._labels([THREAD]))
        self.assertEqual(labels[THREAD]["title"], "聊天对话串")
        self.assertEqual(labels[THREAD]["updated_at"], 1234)
        self.assertEqual(summaries._labels([THREAD]), labels)
        self.assertEqual(self.backend.queries, 1)
        self.assertEqual(db_path.read_bytes(), before)
        restored = WslConnection(self.root / "data", self.backend)
        self.assertEqual(self.candidates(restored)[0]["title"], "聊天对话串")

    def test_metadata_keeps_parent_links_and_versioned_index_fallback(self):
        self.apply()
        self.index(source=json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": PARENT, "agent_path": "/work/build"}}}), name=None)
        label, = self.candidates()
        self.assertEqual(label["parent_thread_id"], PARENT)
        self.assertEqual(label["session_kind"], "subagent")
        self.assertIn("build", label["fallback_title"])
        with closing(sqlite3.connect(self.backend.config_path.parent / "state_42.sqlite")) as db, db:
            db.execute("UPDATE threads SET name=NULL,title=NULL")
        (self.backend.config_path.parent / "session_index.jsonl").write_text(
            json.dumps({"id": THREAD, "thread_name": "indexed title"}) + "\n", encoding="utf8")
        self.wsl.labels.clear()
        self.assertEqual(self.candidates()[0]["title"], "indexed title")

    def test_native_windows_label_is_authoritative(self):
        self.index()
        summaries = SessionSummaries(TokenWindows(self.backend.config_path.parent),
                                     wsl_labels=lambda _: self.fail("Known Windows IDs do not need WSL"))
        self.assertEqual(summaries._labels([THREAD])[THREAD]["title"], "聊天对话串")

    def test_legacy_wsl_history_uses_native_host_and_groups_with_its_parent(self):
        self.index(source=json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}))
        linux = self.backend.config_path.parent
        with closing(sqlite3.connect(linux / "state_42.sqlite")) as db, db:
            db.execute("INSERT INTO threads(id,name,updated_at_ms) VALUES(?,?,?)", (PARENT, "远程父会话", 1234000))
        windows = self.root / "windows"
        windows.mkdir()
        (windows / ".codex-global-state.json").write_text(json.dumps({
            "thread-project-membership-host-ids": {THREAD: WSL_HOST_ID},
        }), encoding="utf8")
        (windows / "session_index.jsonl").write_text(json.dumps({"id": THREAD, "thread_name": "Local copy"}) + "\n",
                                                   encoding="utf8")
        directory = self.root / "capture"
        Store(directory).add({"method": "POST", "path": "/responses", "thread_id": THREAD})
        sidebar = SessionSummaries(TokenWindows(windows), wsl_labels=self.wsl.labels,
                                   ssh_labels=lambda _: self.fail("The native host is WSL"))
        def named_sessions():
            rows = {row["thread_id"]: row for row in sidebar.read(directory)["sessions"]}
            return rows if rows.get(PARENT, {}).get("title") else None
        rows = self.wait_for(named_sessions)
        self.assertEqual(rows[THREAD]["title"], "聊天对话串")
        self.assertEqual(rows[THREAD]["parent_thread_id"], PARENT)
        self.assertEqual(rows[PARENT]["title"], "远程父会话")

    def test_same_id_in_two_distros_requires_a_route_and_skips_stopped_target(self):
        self.index()
        native = self.backend.config_path
        other = self.root / "debian" / "config.toml"
        other.parent.mkdir()
        other.write_text('model="example"\n', encoding="utf8")
        (other.parent / "session_index.jsonl").write_text(json.dumps({"id": THREAD, "thread_name": "Debian copy"}) + "\n",
                                                        encoding="utf8")
        self.backend.distributions.append("Debian-Test")
        with patch.object(self.backend, "config", side_effect=lambda distro, _: native if distro == "Ubuntu-Test" else other), \
             patch.object(self.backend, "labels", wraps=self.backend.labels) as read:
            self.assertEqual({label["title"] for label in self.candidates()}, {"聊天对话串", "Debian copy"})
            target = next(path.name for path, value in self.wsl._targets() if value["distribution"] == "Ubuntu-Test")
            read.reset_mock()
            label, = self.candidates(key=target)
            self.assertEqual(label["title"], "聊天对话串")
            read.assert_called_once_with("Ubuntu-Test", "/home/test/.codex", [THREAD])

            self.backend.distributions = ["Debian-Test"]
            self.wsl.labels.clear()
            read.reset_mock()
            self.assertEqual(self.candidates(key=target), ())
            read.assert_not_called()

    def test_unscoped_history_waits_for_ssh_before_using_a_wsl_name(self):
        ssh_lookup = {}
        wsl_label = {"title": "WSL name"}
        sidebar = SessionSummaries(TokenWindows(self.root / "windows"),
                                   wsl_labels=lambda _: {THREAD: (wsl_label,)}, ssh_labels=lambda _: ssh_lookup)
        self.assertEqual(sidebar._labels([THREAD]), {})
        ssh_lookup[THREAD] = ({"title": "SSH name"},)
        self.assertEqual(sidebar._labels([THREAD]), {})
        ssh_lookup[THREAD] = ()
        self.assertEqual(sidebar._labels([THREAD]), {THREAD: wsl_label})

    def test_home_change_never_reads_old_json_index_for_same_distro(self):
        path = self.backend.config_path.parent / "session_index.jsonl"
        path.write_text(json.dumps({"id": THREAD, "thread_name": "Old account"}) + "\n", encoding="utf8")
        self.apply()
        key = next(self.wsl._targets())[0].name
        with patch.object(self.backend, "labels", side_effect=WslError("codex_home_changed")):
            self.assertEqual(self.candidates(key=key), ())
        self.assertEqual(self.wsl.metadata_error, "codex_home_changed")

    def test_wsl_discovery_decoding_custom_home_and_path_validation(self):
        self.assertEqual(decode_output("Ubuntu\n".encode("utf-16-le")), "Ubuntu\n")
        cli = LocalWsl()
        with patch.object(cli, "run", return_value="notice\nCPI_CODEX_HOME=/srv/codex-home\n"):
            self.assertEqual(cli.home("Ubuntu"), "/srv/codex-home")
        cli.executable = "wsl.exe"
        with patch.object(cli, "run", return_value="Ubuntu\ndocker-desktop\nDebian\n"):
            self.assertEqual(cli.running(), ["Ubuntu", "Debian"])
        for home in ("relative", "/home/../root", "//host/share", "/", "/a\nb"):
            with self.subTest(home=home), self.assertRaises(WslError):
                validate_target("Ubuntu", home)

    def test_probe_checks_instance_with_real_http_and_ignores_proxy_environment(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        marker = hashlib.sha256(ENDPOINT.encode()).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                body = json.dumps({"instance": marker}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cli = LocalWsl()
        bash = Path(r"C:/Program Files/Git/bin/bash.exe") if sys.platform == "win32" else Path("/bin/bash")
        if not bash.is_file():
            self.skipTest("Bash is required for the real TCP probe test")
        def run(arguments, **kwargs):
            self.assertEqual(arguments[-4:-1], ["bash", "-s", "--"])
            result = subprocess.run([str(bash), "-s", "--", arguments[-1]], input=kwargs["data"],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.decode()
        with patch.object(cli, "run", side_effect=run), patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1"}):
            cli.probe("Ubuntu", server.server_port, marker)
            with self.assertRaisesRegex(WslError, "relay_instance_mismatch"):
                cli.probe("Ubuntu", server.server_port, "other-instance")

    def test_metadata_failure_is_bounded_and_retried_after_a_cooldown(self):
        self.apply()
        with patch.object(self.backend, "labels", side_effect=WslError("wsl_timeout")) as query:
            self.assertEqual(self.candidates(), ())
            self.assertEqual(self.candidates(), ())
            self.assertEqual(query.call_count, 1)
        self.assertEqual(self.wsl.metadata_error, "wsl_timeout")

    def test_server_connect_routes_status_and_probe_endpoint(self):
        from packet_inspector.server import InspectorServer
        from urllib.request import urlopen
        config = self.root / "windows.toml"
        config.write_text('model="test"\n', encoding="utf8")
        with patch("packet_inspector.server.repair_loopback_proxy", return_value={"changed": False}), \
                patch("packet_inspector.network_diagnostics.NetworkDiagnostics._probe"):
            server = InspectorServer(("127.0.0.1", 0), self.root / "server", config)
            self.addCleanup(server.server_close)
            server.wsl.backend = self.backend
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.shutdown)
            result = server.set_connection(True)
            self.assertTrue(result["wsl"]["targets"])
            self.assertTrue(server.status()["wsl_connection"]["targets"])
            self.assertGreater(len(server.settings["provider_routes"]), 1)
            with urlopen(server.ui_url + "api/wsl-probe", timeout=3) as response:
                self.assertEqual(json.load(response)["instance"], hashlib.sha256(server.endpoint.encode()).hexdigest())
            self.assertTrue(server.set_connection(False)["wsl"]["targets"])

    def test_relay_captures_wsl_route_and_sidebar_does_not_wait_for_native_query(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from packet_inspector.server import InspectorServer

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = b'{"status":"completed"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.addCleanup(upstream.server_close)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.shutdown)
        config = self.root / "windows.toml"
        config.write_text('model="test"\n', encoding="utf8")
        self.backend.config_path.write_text(f'openai_base_url="http://127.0.0.1:{upstream.server_port}"\n', encoding="utf8")
        self.index()
        with patch("packet_inspector.server.repair_loopback_proxy", return_value={"changed": False}), \
             patch("packet_inspector.network_diagnostics.NetworkDiagnostics._probe"):
            server = InspectorServer(("127.0.0.1", 0), self.root / "server", config)
            self.addCleanup(server.server_close)
            server.wsl.backend = self.backend
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            self.assertTrue(server.set_connection(True)["wsl"]["targets"])

        relay = tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))["openai_base_url"]
        with urlopen(Request(relay + "/responses", b'{"model":"test","input":[]}',
                             {"Content-Type": "application/json", "session_id": THREAD}), timeout=3) as response:
            self.assertEqual(response.status, 200)
        key = next(server.wsl._targets())[0].name
        self.assertEqual(server.store.list(thread=THREAD)[0]["wsl_connection_key"], key)

        entered, release = threading.Event(), threading.Event()
        original = self.backend.labels
        def slow_labels(*args):
            entered.set()
            if not release.wait(4):
                raise WslError("wsl_timeout")
            return original(*args)

        def sessions():
            with urlopen(server.ui_url + "api/session-summaries", timeout=2) as response:
                return json.load(response)["sessions"]

        with patch.object(self.backend, "labels", side_effect=slow_labels):
            try:
                self.assertIsNone(sessions()[0]["title"])
                self.assertTrue(entered.wait(2))
                self.assertIsNone(sessions()[0]["title"])
            finally:
                release.set()
            named = self.wait_for(lambda: [row for row in sessions() if row["title"]])
        self.assertEqual((named[0]["thread_id"], named[0]["title"]), (THREAD, "聊天对话串"))


if __name__ == "__main__":
    unittest.main()
