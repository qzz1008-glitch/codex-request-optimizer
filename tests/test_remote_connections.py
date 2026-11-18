"""Per-host connection ownership, bounded discovery and SSH control contracts."""
from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from packet_inspector.remote_connections import Connections, Host, SshControl, saved_hosts
from packet_inspector.remote_control import SCRIPT, RESULT_PREFIX


class RemoteConnectionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.ssh = self.root / "config"
        self.ssh.write_text("Host server-a\n HostName 192.0.2.1\nHost git.example\nHost *\n", encoding="utf8")
        self.rows = [{"hostId": "remote-ssh-codex-managed:a", "displayName": "Server A", "alias": "server-a"},
                     {"hostId": "remote-ssh-codex-managed:b", "hostname": "192.0.2.2", "sshPort": 2202}]
        self.catalog(self.rows)
        self.local_active = False
        self.local_calls, self.remote_calls = [], []
        self.remote_active = {row["hostId"]: False for row in self.rows}
        self.manager = Connections(self.home, self.profile, self.apply_local, transport=self.transport, ssh_config=self.ssh)
        self.addCleanup(self.manager.close)

    def catalog(self, rows, **extra):
        (self.home / ".codex-global-state.json").write_text(json.dumps({"codex-managed-remote-connections": rows, **extra}), encoding="utf8")

    def profile(self):
        return {"active": self.local_active, "can_restore": self.local_active}

    def apply_local(self, connected):
        self.local_calls.append(connected)
        self.local_active = connected
        return {"reload_required": True}

    def transport(self, host, action, origin):
        self.remote_calls.append((host.id, action))
        if action in {"activate", "restore"}:
            self.remote_active[host.id] = action == "activate"
        result = {"state": "connected" if self.remote_active[host.id] else "disconnected", "error": "",
                  "origin": "http://127.0.0.1:53111", "can_connect": True, "can_disconnect": True}
        if action == "reconnect":
            result["codex_reconnect"] = {"state": "reloaded", "error": ""}
        return result

    def settled(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.manager.snapshot()
            if all(not row.get("busy") for row in result["targets"]):
                return {row["id"]: row for row in result["targets"]}
            time.sleep(.005)
        self.fail("Connection tasks did not settle")

    def test_only_codex_servers_are_discovered_not_unrelated_ssh_or_wsl(self):
        (self.root / "included").write_text("Host server-c\nHost wsl-local\n", encoding="utf8")
        with self.ssh.open("a", encoding="utf8") as stream:
            stream.write("Include included\n")
        self.catalog(self.rows, **{"remote-projects": [{"hostId": "remote-ssh-discovered:server-c"}, {"hostId": "remote-ssh-discovered:wsl-local"}]})
        hosts = saved_hosts(self.home, self.ssh)
        self.assertEqual([host.destination for host in hosts], ["server-a", "192.0.2.2", "server-c"])
        self.assertEqual(hosts[1].port, 2202)

    def test_malformed_and_duplicate_native_entries_cannot_become_ssh_options(self):
        self.catalog([*self.rows, self.rows[0], {"hostId": "remote-ssh-codex-managed:x", "hostname": "-oProxyCommand=bad"},
                      {"hostId": "remote-ssh-codex-managed:y", "hostname": "host;bad"},
                      {"hostId": "remote-ssh-codex-managed:z", "hostname": "valid", "sshPort": True}])
        self.assertEqual(len(saved_hosts(self.home, self.ssh)), 2)

    def test_alias_options_and_explicit_identity_match_native_ssh_resolution(self):
        self.rows[0].update(sshPort=9999, identity="ignored-with-alias")
        self.rows[1]["identity"] = "~/.ssh/remote-key"
        self.catalog(self.rows)
        hosts = saved_hosts(self.home, self.ssh)
        self.assertEqual((hosts[0].port, hosts[0].identity), (None, None))
        self.assertEqual((hosts[1].port, hosts[1].identity), (2202, "~/.ssh/remote-key"))

    def test_probes_do_not_block_or_change_configs_and_are_cached(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args):
            entered.set();release.wait(2)
            return self.transport(*args)
        self.manager.transport = blocked
        started = time.monotonic()
        result = self.manager.snapshot()
        self.assertLess(time.monotonic() - started, .2)
        self.assertTrue(entered.wait(.5))
        self.assertEqual(self.local_calls, [])
        self.assertTrue(any(row.get("busy") for row in result["targets"]))
        release.set();self.settled()
        count = len(self.remote_calls)
        self.settled();self.settled()
        self.assertEqual(len(self.remote_calls), count)
        self.assertTrue(all(action is None for _, action in self.remote_calls))

    def test_one_host_does_not_change_local_or_other_hosts(self):
        self.settled();self.remote_calls.clear()
        identity = self.rows[0]["hostId"]
        result = self.manager.apply([identity], "activate")
        values = self.settled()
        self.assertEqual(values[identity]["state"], "connected")
        self.assertEqual(values[self.rows[1]["hostId"]]["state"], "disconnected")
        self.assertEqual(self.local_calls, [])
        self.assertEqual(self.remote_calls, [(identity, "activate")])
        self.assertEqual(result["accepted"][identity], values[identity]["operation_id"])
        self.assertNotIn("origin", values[identity])

    def test_busy_host_blocks_only_its_own_scope(self):
        self.settled()
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.transport
        def blocked(host, action, origin):
            if host.id == self.rows[0]["hostId"] and action:
                entered.set();release.wait(2)
            return original(host, action, origin)
        self.manager.transport = blocked
        self.manager.apply([self.rows[0]["hostId"]], "activate")
        self.assertTrue(entered.wait(.5))
        with self.assertRaises(ValueError):
            self.manager.apply(["local", self.rows[0]["hostId"]], "activate")
        self.assertEqual(self.local_calls, [])
        self.manager.apply(["local", self.rows[1]["hostId"]], "activate")
        release.set()
        values = self.settled()
        self.assertEqual(self.local_calls, [True])
        self.assertTrue(all(row["state"] == "connected" for row in values.values()))

    def test_bulk_partial_failure_keeps_successful_devices_and_allows_independent_restore(self):
        self.settled()
        original = self.transport
        def partly_fails(host, action, origin):
            if host.id == self.rows[1]["hostId"] and action:
                raise ValueError("SSH failed")
            return original(host, action, origin)
        self.manager.transport = partly_fails
        self.manager.apply(["local", *(row["hostId"] for row in self.rows)], "activate")
        values = self.settled()
        self.assertEqual([values[key]["state"] for key in values], ["connected", "connected", "error"])
        self.assertEqual(self.local_calls, [True])
        self.manager.apply([self.rows[0]["hostId"]], "restore")
        self.assertEqual(self.settled()[self.rows[0]["hostId"]]["state"], "disconnected")
        self.assertTrue(self.local_active)

    def test_stale_unknown_duplicate_or_busy_scope_is_rejected_before_any_write(self):
        self.settled()
        for identities in [["local", "gone"], ["local", "local"], [], [1]]:
            with self.subTest(identities=identities), self.assertRaises(ValueError):
                self.manager.apply(identities, "activate")
        self.assertEqual(self.local_calls, [])
        self.catalog([])
        with self.assertRaises(ValueError):
            self.manager.apply(["local", self.rows[0]["hostId"]], "activate")
        self.assertEqual(self.local_calls, [])

    def test_local_uses_existing_reconciliation_even_if_primary_config_already_matches(self):
        self.settled();self.manager.apply(["local"], "restore");self.settled()
        self.assertEqual(self.local_calls, [False])
        self.manager.close()
        with self.assertRaises(ValueError):
            self.manager.apply(["local"], "activate")

    def test_local_action_errors_survive_fresh_profile_reads(self):
        self.settled()
        def fail(_connected):
            raise ValueError("Local configuration is read-only")
        self.manager.local_apply = fail
        self.manager.apply(["local"], "activate")
        self.assertEqual(self.settled()["local"]["error"], "Local configuration is read-only")
        self.assertTrue(self.settled()["local"]["error"])

    def test_confirmation_revision_rejects_a_replaced_host_with_the_same_id(self):
        self.settled();revision = self.manager.snapshot()["revision"]
        self.rows[0]["alias"] = "replacement-server"
        self.catalog(self.rows)
        with self.assertRaisesRegex(ValueError, "影响范围"):
            self.manager.apply(["local", self.rows[0]["hostId"]], "activate", revision=revision)
        self.assertEqual(self.local_calls, [])

    def test_port_hint_survives_restart_without_caching_relay_capabilities(self):
        cache = self.root / "cache" / "remote-connections.json"
        self.manager.cache_path = cache
        self.settled()
        self.assertIn("53111", cache.read_text())
        following = Connections(self.home, self.profile, self.apply_local, transport=self.transport,
                                ssh_config=self.ssh, cache_path=cache)
        self.addCleanup(following.close)
        with following.guard:
            following._discover()
            self.assertEqual(following.entries[self.rows[0]["hostId"]]["origin"], "http://127.0.0.1:53111")
        self.assertNotIn("/relay/", cache.read_text())

    def test_corrupt_catalog_remains_an_explicit_error(self):
        (self.home / ".codex-global-state.json").write_text("{", encoding="utf8")
        value = self.manager.snapshot()
        self.assertTrue(value["error"])
        self.assertEqual(value["targets"][0]["id"], "local")
        with self.assertRaises(ValueError):
            self.manager.apply(["local"], "activate")

    def test_ssh_uses_fixed_remote_command_batch_auth_and_no_shell(self):
        value = RESULT_PREFIX + json.dumps({"state": "connected"})
        result = SimpleNamespace(returncode=0, stdout=("banner\n" + value).encode(), stderr=b"")
        with patch("packet_inspector.remote_connections.shutil.which", return_value="ssh"), \
             patch("packet_inspector.remote_connections.subprocess.run", return_value=result) as run:
            self.assertEqual(SshControl()(Host("id", "name", "root@host", 2202), "activate")["state"], "connected")
        args, kwargs = run.call_args
        self.assertEqual(args[0][-3:], ["--", "root@host", "exec \"${SHELL:-/bin/sh}\" -lc 'cd / && exec python3 -I -'"])
        self.assertIn("BatchMode=yes", args[0])
        self.assertNotIn("StrictHostKeyChecking=no", args[0])
        self.assertNotIn("shell", kwargs)
        self.assertIn(b'"action": "activate"', kwargs["input"])
        self.assertEqual(kwargs["timeout"], 90)

    def test_ssh_timeout_never_replays_uncertain_config_changes(self):
        with patch("packet_inspector.remote_connections.shutil.which", return_value="ssh"), \
             patch("packet_inspector.remote_connections.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 90)) as run:
            with self.assertRaisesRegex(ValueError, "未确认"):
                SshControl()(Host("id", "name", "host"), "activate")
        self.assertEqual(run.call_count, 1)

    def test_reconnect_targets_one_remote_without_changing_provider_or_local_state(self):
        identity = self.rows[0]["hostId"]
        self.remote_active[identity] = True
        self.settled()
        self.remote_calls.clear()
        self.manager.apply([identity], "reconnect")
        result = self.settled()[identity]
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["codex_reconnect"]["state"], "reloaded")
        self.assertEqual(self.remote_calls, [(identity, "reconnect")])
        self.assertEqual(self.local_calls, [])
        self.assertTrue(self.remote_active[identity])

    def test_reconnect_rejects_local_and_bulk_operations(self):
        self.settled()
        self.remote_calls.clear()
        for identities in (["local"], [row["hostId"] for row in self.rows]):
            with self.assertRaisesRegex(ValueError, "一台服务器"):
                self.manager.apply(identities, "reconnect")
        self.assertEqual(self.remote_calls, [])
        self.assertEqual(self.local_calls, [])

    def test_reconnect_transport_failure_is_unconfirmed_and_keeps_provider_state(self):
        identity = self.rows[0]["hostId"]
        self.remote_active[identity] = True
        self.settled()
        self.manager.entries[identity]["reload_required"] = True
        self.manager.transport = lambda *_: (_ for _ in ()).throw(ValueError("SSH result unknown"))
        self.manager.apply([identity], "reconnect")
        result = self.settled()[identity]
        self.assertEqual(result["state"], "connected")
        self.assertTrue(result["reload_required"])
        self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
        self.assertIn("SSH result unknown", result["codex_reconnect"]["error"])


class RemoteScriptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "config.toml").write_text('openai_base_url="http://127.0.0.1:53111/relay/secret/p/one"\n', encoding="utf8")
        self.status = {"version": "test", "connection_policy": 1,
                       "profile": {"config_path": str(self.root / "config.toml"), "active": False, "can_restore": False}}
        self.calls = []
        self.native = False
        self.native_mode = "ready"
        self.native_calls = []
        self.native_cli = self.root / "native-codex"
        self.native_cli.write_text("fixture", encoding="utf8")
        self.socket = self.root / "app-server-control" / "control.sock"

    def execute(self, action=None):
        test = self
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        class Opener:
            def open(self, request, **kwargs):
                test.calls.append((request.full_url, request.data))
                if request.data:
                    body = json.loads(request.data)
                    if request.full_url.endswith("/api/restart-notice"):
                        test.assertEqual(body, {"target": "chatgpt", "revision": test.status["restart_notice"]["revision"]})
                        test.status["restart_notice"]["revision"] += 1
                        test.status["restart_notice"]["items"] = [item for item in test.status["restart_notice"]["items"] if item["target"] != "chatgpt"]
                        return Response(json.dumps(test.status["restart_notice"]).encode())
                    test.status["profile"].update(active=body["action"] == "activate", can_restore=True)
                    return Response(json.dumps({"reload_required": True}).encode())
                return Response(json.dumps(test.status).encode())
        def native_run(args, **kwargs):
            self.native_calls.append((args, kwargs))
            action = args[-1]
            if action == "restart":
                if self.native_mode == "timeout":
                    raise subprocess.TimeoutExpired(args, 120)
                if self.native_mode == "failure":
                    return SimpleNamespace(returncode=1, stdout=b"", stderr=b"native restart denied")
                if self.native_mode in {"notice-changed", "changed"}:
                    self.status["restart_notice"]["revision"] += 1
                if self.native_mode in {"config-changed", "changed"}:
                    with (self.root / "config.toml").open("a") as stream:
                        stream.write('# changed while restarting\n')
            restart_started = any(call[0][-1] == "restart" for call in self.native_calls)
            ready = not (self.native_mode == "not-ready" and restart_started)
            socket = self.root.parent / "other-home.sock" if self.native_mode == "wrong-home" else self.socket
            value = {"status": "restarted" if action == "restart" else "running" if ready else "notRunning",
                     "managedCodexPath": str(self.native_cli), "socketPath": str(socket)}
            if self.native_mode != "unmanaged" and not (
                    self.native_mode == "unmanaged-after-restart" and action == "version" and restart_started):
                value["backend"] = "pid"
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode(), stderr=b"")
        original_resolve = Path.resolve
        original_stat = Path.stat
        def resolve_path(path, *args, **kwargs):
            if path == self.socket and self.native_mode == "linked-socket":
                return self.root.parent / "native-runtime.sock"
            return original_resolve(path, *args, **kwargs)
        def stat_path(path, *args, **kwargs):
            if path == self.socket:
                mode = stat.S_IFREG if self.native_mode == "regular-file" else stat.S_IFSOCK
                uid = 1001 if self.native_mode == "wrong-owner" else 1000
                return SimpleNamespace(st_mode=mode, st_uid=uid)
            return original_stat(path, *args, **kwargs)
        output = io.StringIO()
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}), \
             patch("os.getuid", return_value=1000, create=True), \
             patch("pathlib.Path.home", return_value=self.root), \
             patch("pathlib.Path.resolve", new=resolve_path), \
             patch("pathlib.Path.stat", new=stat_path), \
             patch("shutil.which", return_value=str(self.native_cli) if self.native else None), \
             patch("subprocess.run", side_effect=native_run), \
             patch("urllib.request.build_opener", return_value=Opener()) as build, redirect_stdout(output):
            exec("INPUT = " + repr(json.dumps({"action": action})) + "\n" + SCRIPT, {})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertNotIn("secret", output.getvalue())
        return json.loads(output.getvalue().split(RESULT_PREFIX)[-1])

    def test_read_only_probe_and_restore_reconcile_remote_saved_intent(self):
        self.assertEqual(self.execute()["state"], "disconnected")
        self.assertFalse(any(body for _, body in self.calls))
        self.calls.clear();self.execute("restore")
        self.assertTrue(any(body for _, body in self.calls))

    def test_action_reuses_remote_api_and_reports_reload(self):
        result = self.execute("activate")
        self.assertEqual(result["state"], "connected")
        self.assertTrue(result["reload_required"])
        writes = [(url, json.loads(body)) for url, body in self.calls if body]
        self.assertEqual(writes, [("http://127.0.0.1:53111/api/link", {"action": "activate"})])
        self.assertNotIn("/relay/", result["origin"])

    def test_wrong_codex_home_or_unrelated_service_is_never_modified(self):
        self.status["profile"]["config_path"] = str(self.root / "another.toml")
        self.assertEqual(self.execute("activate")["state"], "error")
        self.assertFalse(any(body for _, body in self.calls))

    def enable_native(self):
        self.native = True
        self.status["profile"]["active"] = True
        self.status["restart_notice"] = {"revision": 4, "error": "", "items": [
            {"key": "connection", "target": "chatgpt", "scope": ""},
            {"key": "wsl", "target": "wsl", "scope": "distribution"}]}

    @contextmanager
    def ssh_app_server(self, *, invalid=None, exits=True, replacement=True):
        self.enable_native()
        self.native_mode = "unmanaged"
        self.socket.parent.mkdir(exist_ok=True)
        self.socket.touch()
        old_pid, new_pid = 2100, 2200
        peer_pid = old_pid
        proc = Path("/proc")
        original_open = Path.open
        home = self.root.parent if invalid == "home" else self.root
        cmdline = b"codex\0-c\0features.code_mode_host=true\0app-server\0--listen\0unix://\0"
        samples = {
            "cmdline": cmdline.replace(b"unix://", b"stdio://") if invalid == "transport" else cmdline,
            "environ": b"CODEX_HOME=" + str(home).encode("utf-8") + b"\0",
            "status": "SigCgt:\t0\n" if invalid == "signal" else "SigCgt:\t1\n",
        }

        def open_path(path, *args, **kwargs):
            if path.parent.parent == proc:
                value = samples[path.name]
                return io.StringIO(value) if isinstance(value, str) else io.BytesIO(value)
            return original_open(path, *args, **kwargs)

        class Peer:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def connect(self, address):
                if peer_pid is None:
                    raise ConnectionRefusedError()
            def getsockopt(self, *args):
                return struct.pack("iII", peer_pid, 1001 if invalid == "uid" else 1000, 1000)
            def sendall(self, data): pass
            def makefile(self, mode):
                return io.BytesIO(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            def recv(self, size): return b"\x88\x00"

        def stop_backend(*args):
            nonlocal peer_pid
            if exits:
                peer_pid = new_pid if replacement else None

        send_signal = Mock(side_effect=stop_backend)
        closed_fds = Mock()
        poller = Mock()
        poller.poll.return_value = [(old_pid + 10000, 1)] if exits else []
        with (
            patch("pathlib.Path.open", new=open_path),
            patch("pathlib.Path.samefile", return_value=invalid != "exe"),
            patch("os.pidfd_open", side_effect=lambda pid: pid + 10000, create=True),
            patch("os.close", closed_fds),
            patch("select.poll", return_value=poller, create=True),
            patch("select.POLLIN", 1, create=True),
            patch("signal.pidfd_send_signal", send_signal, create=True),
            patch("signal.SIGHUP", 1, create=True),
            patch("socket.socket", return_value=Peer()),
            patch("socket.AF_UNIX", 1, create=True),
            patch("socket.SO_PEERCRED", 17, create=True),
        ):
            yield send_signal, closed_fds

    def test_native_reconnect_verifies_ready_then_acknowledges_only_current_account(self):
        self.enable_native()
        result = self.execute("reconnect")
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["codex_reconnect"]["state"], "reloaded")
        self.assertFalse(result["reload_required"])
        self.assertEqual([args[-1] for args, _ in self.native_calls], ["version", "restart", "version"])
        for args, options in self.native_calls:
            self.assertEqual(args[:4], [str(self.native_cli), "app-server", "daemon", args[-1]])
            self.assertEqual(options["env"]["CODEX_HOME"], str(self.root))
        writes = [(url, json.loads(body)) for url, body in self.calls if body]
        self.assertEqual(writes, [("http://127.0.0.1:53111/api/restart-notice", {"target": "chatgpt", "revision": 4})])
        self.assertEqual(self.status["restart_notice"]["items"][0]["target"], "wsl")

    def test_detects_native_socket_link_to_runtime_directory(self):
        self.enable_native()
        self.native_mode = "linked-socket"
        result = self.execute()
        self.assertTrue(result["codex_restart"]["supported"])
        self.assertTrue(result["reload_required"])
        self.assertEqual([args[-1] for args, _ in self.native_calls], ["version"])

    def test_unverified_app_server_never_offers_or_attempts_restart(self):
        self.enable_native()
        self.native_mode = "unmanaged"
        probe = self.execute()
        self.assertFalse(probe["codex_restart"]["supported"])
        self.assertTrue(probe["reload_required"])

        result = self.execute("reconnect")
        self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
        self.assertTrue(result["reload_required"])
        self.assertEqual([args[-1] for args, _ in self.native_calls], ["version", "version"])
        self.assertFalse(any(body for _, body in self.calls))

    def test_ordinary_ssh_reconnect_waits_for_replacement_before_acknowledging(self):
        with self.ssh_app_server() as (send_signal, closed_fds):
            probe = self.execute()
            self.assertTrue(probe["codex_restart"]["supported"])
            self.assertTrue(probe["reload_required"])
            send_signal.assert_not_called()
            result = self.execute("reconnect")
            send_signal.assert_called_once_with(12100, signal.SIGHUP)
            self.assertEqual([call.args[0] for call in closed_fds.call_args_list], [12100, 12100, 12200, 12100])
        self.assertEqual(result["codex_reconnect"]["state"], "reloaded")
        self.assertFalse(result["reload_required"])
        self.assertEqual([args[-1] for args, _ in self.native_calls], ["version", "version", "version"])
        self.assertEqual(self.status["restart_notice"]["items"][0]["target"], "wsl")

    def test_ssh_reconnect_rejects_an_unrelated_or_unverifiable_listener(self):
        for invalid in ("uid", "exe", "home", "transport", "signal"):
            with self.subTest(invalid=invalid), self.ssh_app_server(invalid=invalid) as (send_signal, _):
                self.calls.clear()
                result = self.execute("reconnect")
                send_signal.assert_not_called()
                self.assertFalse(result["codex_restart"]["supported"])
                self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
                self.assertTrue(result["reload_required"])
                self.assertFalse(any(body for _, body in self.calls))

    def test_ssh_reconnect_timeout_preserves_pending_configuration(self):
        for exits in (False, True):
            with self.subTest(exits=exits), self.ssh_app_server(exits=exits, replacement=False) as (send_signal, _):
                self.calls.clear()
                with patch("time.monotonic", side_effect=(0, 99, 100, 101)), patch("time.sleep"):
                    result = self.execute("reconnect")
                send_signal.assert_called_once()
                self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
                self.assertTrue(result["reload_required"])
                self.assertFalse(any(body for _, body in self.calls))

    def test_unsupported_daemon_never_restarts_or_acknowledges(self):
        for mode in ("missing", "wrong-home", "wrong-owner", "regular-file"):
            with self.subTest(mode=mode):
                self.enable_native()
                self.native = mode != "missing"
                self.native_mode = mode
                self.calls.clear();self.native_calls.clear()
                result = self.execute("reconnect")
                self.assertFalse(result["codex_restart"]["supported"])
                self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
                self.assertTrue(result["reload_required"])
                self.assertFalse(any(args[-1] == "restart" for args, _ in self.native_calls))
                self.assertFalse(any(body for _, body in self.calls))

    def test_failure_timeout_or_not_ready_does_not_clear_pending_configuration(self):
        for mode in ("failure", "timeout", "not-ready", "unmanaged-after-restart"):
            with self.subTest(mode=mode):
                self.enable_native();self.native_mode = mode
                self.calls.clear();self.native_calls.clear()
                result = self.execute("reconnect")
                self.assertEqual(result["state"], "connected")
                self.assertEqual(result["codex_reconnect"]["state"], "unconfirmed")
                self.assertTrue(result["reload_required"])
                self.assertEqual(sum(args[-1] == "restart" for args, _ in self.native_calls), 1)
                self.assertFalse(any(body for _, body in self.calls))

    def test_concurrent_configuration_change_retains_new_notice(self):
        for mode in ("notice-changed", "config-changed", "changed"):
            with self.subTest(mode=mode):
                self.enable_native();self.native_mode = mode
                self.calls.clear();self.native_calls.clear()
                result = self.execute("reconnect")
                self.assertEqual(result["codex_reconnect"]["state"], "reloaded")
                self.assertIn("配置又有变化", result["codex_reconnect"]["warning"])
                self.assertTrue(result["reload_required"])
                self.assertFalse(any(body for _, body in self.calls))


class ConnectionApiTests(unittest.TestCase):
    def test_real_api_connects_restores_and_keeps_relay_serving_with_origin_checks(self):
        from packet_inspector.server import InspectorServer
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('model="test"\n', encoding="utf8")
            server = InspectorServer(("127.0.0.1", 0), root / "data", config)
            server.connections.ssh_config = root / "unused-ssh-config"
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            opener = build_opener(ProxyHandler({}))
            endpoint = "http://127.0.0.1:" + str(server.port)
            def request(path, data=None, origin=None):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                req = Request(endpoint + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
                with opener.open(req, timeout=5) as response:
                    return response.status, json.load(response)
            try:
                with patch.object(server.wsl, "apply", return_value={"changed": False, "reload_required": False, "targets": [], "warnings": []}), \
                     patch("packet_inspector.server.repair_loopback_proxy", return_value={"changed": False}), \
                     patch.object(server.network_diagnostics, "connection"):
                    code, snapshot = request("/api/connections")
                    self.assertEqual(code, 200)
                    self.assertEqual([row["id"] for row in snapshot["targets"]], ["local"])
                    with self.assertRaises(HTTPError) as denied:
                        request("/api/connections", {"targets": ["local"], "action": "activate", "revision": snapshot["revision"]}, "https://external.invalid")
                    self.assertEqual(denied.exception.code, 403)
                    denied.exception.close()
                    self.assertFalse(server.connection_profile()["active"])
                    for action, connected in [("activate", True), ("restore", False)]:
                        code, accepted = request("/api/connections", {"targets": ["local"], "action": action, "revision": snapshot["revision"]})
                        self.assertEqual(code, 202)
                        self.assertIn("local", accepted["accepted"])
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            _, following = request("/api/connections")
                            if not following["targets"][0].get("busy"):
                                break
                            time.sleep(.01)
                        self.assertEqual(server.connection_profile()["active"], connected)
                        self.assertEqual(server.settings["auto_detect_providers"], connected)
                        self.assertEqual(request("/api/status")[0], 200)
                    with self.assertRaises(HTTPError) as stale:
                        request("/api/connections", {"targets": ["local"], "action": "activate", "revision": "stale"})
                    self.assertEqual(stale.exception.code, 400)
                    stale.exception.close()
                    self.assertFalse(server.connection_profile()["active"])
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()


if __name__ == "__main__":
    unittest.main()
