"""UI iteration must preserve the independently running forwarding service."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.build_inspector import output_directory
from packet_inspector import VERSION
from packet_inspector.desktop_ui import DesktopUI


class LegacyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        if self.path == "/business-stream":
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"a")
            self.wfile.flush()
            self.server.started.set()
            self.server.release.wait(5)
            self.wfile.write(b"bcdef")
            return
        if body == b"drop-response":
            self.close_connection = True
            return
        if self.path == "/health":
            value = {"app": "codex-packet-inspector", "version": "0.2.0"}
        elif self.path == "/api/status":
            value = {"version": "0.2.0", "active_requests": 1}
            if hasattr(self.server, "test_data_dir"):
                value["data_dir"] = self.server.test_data_dir
        elif self.path == "/api/packets/" + "a" * 32:
            value = self.server.test_packet
        elif self.path.startswith("/api/packets?") and hasattr(self.server, "test_packet"):
            value = getattr(self.server, "test_following", [{"created": 103}])
        else:
            value = {"received": body.decode()}
        raw = json.dumps(value).encode()
        self.send_response(404 if self.path == "/api/packets/missing" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = handle_request
    do_POST = handle_request


class DesktopUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        for name, text in {"index.html": "<html><head></head><body>new UI</body></html>",
                           "app.js": "'use strict';", "i18n.js": "'use strict';", "style.css": "body { color: black; }",
                           "tray.html": "<html><head></head><body>tray</body></html>",
                           "tray.js": "'use strict';", "tray.css": "body { background: white; }"}.items():
            (self.folder / name).write_text(text, encoding="utf8")
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), LegacyHandler)
        self.backend.daemon_threads = True
        self.backend.requests = []
        self.backend.started = threading.Event()
        self.backend.release = threading.Event()
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.backend_thread.start()
        self.ui = DesktopUI(self.backend.server_port, ui_dir=self.folder).start()

    def tearDown(self):
        self.backend.release.set()
        if self.ui is not None:
            self.ui.close()
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_new_ui_uses_legacy_service_and_reports_both_versions(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"new UI", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.backend.requests, [])
        status, body, _headers = self.request("/api/status")
        data = json.loads(body)
        self.assertEqual((status, data["version"], data["ui_version"]), (200, "0.2.0", VERSION))
        self.assertEqual(data["active_requests"], 1)

    def test_token_window_is_read_locally_without_new_backend_or_writes(self):
        self.backend.test_packet = {"id": "a" * 32, "thread_id": "thread-test", "created": 100}
        expected = {"state": "available", "used_tokens": 40500, "input_tokens": 40000}
        with patch.object(self.ui.token_windows, "lookup", return_value=expected) as lookup:
            status, body, _headers = self.request("/api/packets/" + "a" * 32 + "/token-window")
        self.assertEqual((status, json.loads(body)), (200, expected))
        lookup.assert_called_once_with(self.backend.test_packet, 103)
        self.assertEqual(len(self.backend.requests), 2)
        self.assertTrue(all(r[0] == "GET" and "/token-window" not in r[1] for r in self.backend.requests))

    def test_token_window_obeys_origin_validation(self):
        status, _body, _headers = self.request("/api/packets/" + "a" * 32 + "/token-window",
                                              headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.requests, [])

    def test_tool_search_has_no_model_usage_and_does_not_scan_history(self):
        self.backend.test_packet = {"id": "a" * 32, "method": "POST", "path": "/alpha/search", "created": 100}
        with patch.object(self.ui.token_windows, "lookup_ready") as lookup:
            status, body, _ = self.request("/api/packets/" + "a" * 32 + "/token-window")
        self.assertEqual((status, json.loads(body)), (200, {"state": "unavailable"}))
        lookup.assert_not_called()
        self.assertEqual(len(self.backend.requests), 1)

    def test_tool_search_does_not_cut_off_the_preceding_model_usage(self):
        self.backend.test_packet = {"id": "a" * 32, "method": "POST", "path": "/responses", "created": 100}
        self.backend.test_following = [
            {"method": "POST", "path": "/alpha/search", "created": 101},
            {"method": "POST", "path": "/responses", "is_compaction": True, "created": 103},
        ]
        expected = {"state": "available", "input_tokens": 237211}
        with patch.object(self.ui.token_windows, "lookup_ready", return_value=expected) as lookup:
            status, body, _ = self.request("/api/packets/" + "a" * 32 + "/token-window")
        self.assertEqual((status, json.loads(body)), (200, expected))
        lookup.assert_called_once_with(self.backend.test_packet, 103)

    def test_ambiguous_boundary_after_many_auxiliary_requests_is_not_guessed(self):
        self.backend.test_packet = {"id": "a" * 32, "method": "POST", "path": "/responses", "created": 100}
        self.backend.test_following = [{"method": "POST", "path": "/alpha/search", "created": 101+i} for i in range(32)]
        with patch.object(self.ui.token_windows, "lookup_ready") as lookup:
            status, body, _ = self.request("/api/packets/" + "a" * 32 + "/token-window")
        self.assertEqual((status, json.loads(body)), (200, {"state": "unavailable"}))
        lookup.assert_not_called()

    def test_session_summaries_are_local_read_only_metadata(self):
        self.backend.test_data_dir = str(self.folder)
        expected = {"state": "available", "sessions": [{"thread_id": "thread-test", "title": "会话名称"}]}
        with patch.object(self.ui.session_summaries, "read", return_value=expected) as read:
            status, body, _headers = self.request("/api/session-summaries")
        self.assertEqual((status, json.loads(body)), (200, expected))
        read.assert_called_once_with(str(self.folder))
        self.assertEqual([(r[0], r[1]) for r in self.backend.requests], [("GET", "/api/status")])

    def test_ui_request_origin_is_translated_only_after_local_validation(self):
        status, _body, _headers = self.request("/api/settings", "POST", b'{"budget_bytes":1000000}',
                                             {"Origin": self.ui.ui_url.rstrip("/")})
        self.assertEqual(status, 200)
        request = self.backend.requests[-1]
        self.assertEqual(request[2]["Origin"], f"http://127.0.0.1:{self.backend.server_port}")
        self.assertEqual(request[3], b'{"budget_bytes":1000000}')
        self.assertEqual(self.request("/api/settings", "POST", b"{}", {"Origin": "https://outside.invalid"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Host": "outside.invalid"})[0], 403)
        self.assertEqual(len(self.backend.requests), 1)

    def test_rejected_request_does_not_wait_for_a_missing_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=2)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/settings")
        connection.putheader("Origin", "https://outside.invalid")
        connection.putheader("Content-Length", "4096")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read()), {"error": "local_origin_required"})
        self.assertEqual(self.backend.requests, [])

    def test_lifecycle_and_business_paths_are_not_proxied(self):
        for path in ["/api/stop", "/api/installation", "/relay/fake/responses", "/business-stream"]:
            self.assertEqual(self.request(path, "POST", b"{}")[0], 404)
        self.assertEqual(self.backend.requests, [])

    def test_absolute_urls_are_rejected(self):
        self.assertEqual(self.request("http://outside.invalid/api/status", headers={"Host": f"127.0.0.1:{self.ui.port}"})[0], 400)
        self.assertEqual(self.backend.requests, [])

    def test_oversize_ui_write_is_rejected_before_forwarding(self):
        self.assertEqual(self.request("/api/settings", "POST", b"x" * 8193)[0], 400)
        self.assertEqual(self.backend.requests, [])

    def test_directory_open_uses_the_desktop_without_forwarding_or_saving(self):
        directory = self.folder / "日志 & cache"
        directory.mkdir()
        with patch("packet_inspector.desktop_ui.os.startfile", create=True) as opener:
            status, body, _ = self.request("/api/open-directory", "POST",
                                           json.dumps({"path": str(directory)}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"opened": True, "path": str(directory.resolve())})
        opener.assert_called_once_with(str(directory.resolve()), "explore")
        self.assertEqual(self.backend.requests, [])
        self.assertFalse((self.folder / "settings.json").exists())

    def test_directory_open_rejects_files_missing_paths_and_invalid_input(self):
        missing = self.folder / "missing"
        values = [str(self.folder / "app.js"), str(missing), "relative/folder", "", None, 42]
        with patch("packet_inspector.desktop_ui.os.startfile", create=True) as opener:
            for value in values:
                with self.subTest(value=value):
                    status, body, _ = self.request("/api/open-directory", "POST",
                                                   json.dumps({"path": value}).encode())
                    self.assertEqual(status, 400)
                    self.assertIn("error", json.loads(body))
            opener.assert_not_called()
        self.assertFalse(missing.exists())
        self.assertEqual(self.backend.requests, [])

    def test_directory_open_validates_origin_method_and_body_before_opening(self):
        body = json.dumps({"path": str(self.folder)}).encode()
        with patch("packet_inspector.desktop_ui.os.startfile", create=True) as opener:
            self.assertEqual(self.request("/api/open-directory", "POST", body,
                                          {"Origin": "http://outside.invalid"})[0], 403)
            self.assertEqual(self.request("/api/open-directory")[0], 404)
            for invalid in [b"[]", b"{", b"", b"x" * 8193]:
                self.assertEqual(self.request("/api/open-directory", "POST", invalid)[0], 400)
            opener.assert_not_called()
        self.assertEqual(self.backend.requests, [])

    def test_directory_open_failure_is_reported_without_forwarding(self):
        with patch("packet_inspector.desktop_ui.os.startfile", side_effect=OSError("unavailable"), create=True):
            status, body, _ = self.request("/api/open-directory", "POST",
                                           json.dumps({"path": str(self.folder)}).encode())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "无法打开目录，请检查路径和权限。"})
        self.assertEqual(self.backend.requests, [])

    def test_directory_open_capability_comes_from_the_desktop_and_supports_legacy_relays(self):
        with patch("packet_inspector.desktop_ui.os.startfile", create=True):
            status, body, _ = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["open_directory_supported"])
        self.assertEqual(json.loads(body)["version"], "0.2.0")
        with patch("packet_inspector.desktop_ui.os.startfile", None, create=True):
            self.assertFalse(json.loads(self.request("/api/status")[1])["open_directory_supported"])
            status, _, _ = self.request("/api/open-directory", "POST",
                                        json.dumps({"path": str(self.folder)}).encode())
        self.assertEqual(status, 400)

    def test_backend_error_status_is_preserved(self):
        self.assertEqual(self.request("/api/packets/missing")[0], 404)

    def test_failed_post_is_not_retried(self):
        self.assertEqual(self.request("/api/settings", "POST", b"drop-response")[0], 502)
        self.assertEqual(len(self.backend.requests), 1)

    def test_file_changes_are_visible_and_change_live_reload_fingerprint(self):
        self.ui.live_reload = True
        before = json.loads(self.request("/__dev__/state")[1])["build"]
        page = self.request("/")[1]
        self.assertIn(b'/__dev__/reload.js', page)
        self.assertIn(before.encode(), self.request("/__dev__/reload.js")[1])
        (self.folder / "style.css").write_text("body { color: blue; }", encoding="utf8")
        after = json.loads(self.request("/__dev__/state")[1])["build"]
        self.assertNotEqual(before, after)
        self.assertIn(b"blue", self.request("/style.css")[1])
        self.assertEqual(self.backend.requests, [])

    def test_dev_endpoints_are_absent_from_regular_desktop_ui(self):
        self.assertEqual(self.request("/__dev__/state")[0], 404)
        self.assertNotIn(b'/__dev__/reload.js', self.request("/")[1])

    def test_closing_ui_does_not_interrupt_an_inflight_business_request(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.backend.server_port, timeout=4)
        try:
            connection.request("GET", "/business-stream")
            response = connection.getresponse()
            self.assertEqual(response.read(1), b"a")
            self.assertTrue(self.backend.started.wait(2))
            self.ui.close()
            self.ui = None
            self.backend.release.set()
            self.assertEqual(response.read(), b"bcdef")
            self.assertTrue(self.backend_thread.is_alive())
        finally:
            connection.close()


class DevelopmentSafetyTests(unittest.TestCase):
    def test_default_builds_use_distinct_version_directories(self):
        first, second = output_directory(), output_directory()
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith(VERSION + "-"))
        self.assertFalse(first.exists())

    def test_existing_release_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "CodexPacketInspector.exe"
            executable.write_bytes(b"in-use-release")
            with self.assertRaises(ValueError):
                output_directory(directory)
            self.assertEqual(executable.read_bytes(), b"in-use-release")

    @unittest.skipUnless(os.name == "nt", "Windows installation helper")
    def test_upgrade_helper_rejects_unknown_exe_without_mutating_running_services_or_tasks(self):
        helper = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "update_inspector_service.ps1"
        wrapper = r'''
param($Helper, $Executable, $Case)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$global:inspectorTestMutated = $false
$global:inspectorTestCase = $Case
$global:inspectorTestInstalled = Join-Path $env:USERPROFILE '.codex-packet-inspector\bin\CodexPacketInspector.exe'
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($global:inspectorTestCase -eq 'task') { [pscustomobject]@{State='Running'; Actions=@([pscustomobject]@{Execute=$global:inspectorTestInstalled})} }
}
function Get-NetTCPConnection {
    param($LocalPort, $State, $ErrorAction)
    if ($global:inspectorTestCase -eq 'listener') { [pscustomobject]@{LocalPort=47844; OwningProcess=12345} }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($global:inspectorTestCase -eq 'process') { [pscustomobject]@{ExecutablePath=$global:inspectorTestInstalled} }
}
function Stop-ScheduledTask { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
function Stop-Process { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
function New-Item { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
function Copy-Item { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
function Register-ScheduledTask { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
function Start-ScheduledTask { $global:inspectorTestMutated=$true; throw 'mutation attempted' }
try { & $Helper -SourceExecutable $Executable; throw 'installer unexpectedly continued' }
catch { [pscustomobject]@{mutated=$global:inspectorTestMutated; reason=$_.Exception.Message} | ConvertTo-Json -Compress }
'''
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            script = folder / "guard-check.ps1"
            script.write_text(wrapper, encoding="utf8")
            source = folder / "test.exe"
            source.write_bytes(b"synthetic")
            for case in ("listener", "task", "process"):
                with self.subTest(case=case):
                    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                             "-File", str(script), str(helper), str(source), case],
                                            capture_output=True, encoding="utf8", timeout=15,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    data = json.loads(result.stdout)
                    self.assertFalse(data["mutated"])
                    self.assertIn("Use CodexPacketInspector.exe", data["reason"])


class DesktopProcessLifetimeTests(unittest.TestCase):
    def test_independent_launch_resets_inherited_onefile_environment(self):
        from packet_inspector import desktop

        inherited = {"PYINSTALLER_RESET_ENVIRONMENT": "0",
                     "_PYI_APPLICATION_HOME_DIR": "removed-parent-extraction",
                     "INSPECTOR_LIFETIME_TEST": "preserve-me"}
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.dict(os.environ, inherited), \
                    patch.object(desktop.sys, "frozen", frozen, create=True), \
                    patch.object(desktop.subprocess, "Popen") as popen:
                child = desktop.start_process(["--background", "--tray", "--port", "12345"])
                self.assertIs(child, popen.return_value)
                command = popen.call_args.args[0]
                prefix = [desktop.sys.executable] + ([] if frozen else ["-m", "packet_inspector"])
                self.assertEqual(command, prefix + ["--background", "--tray", "--port", "12345"])
                child_environment = popen.call_args.kwargs["env"]
                self.assertEqual(child_environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
                self.assertEqual(child_environment["INSPECTOR_LIFETIME_TEST"], "preserve-me")
                self.assertEqual(os.environ["PYINSTALLER_RESET_ENVIRONMENT"], "0")
                self.assertIsNot(child_environment, os.environ)


if __name__ == "__main__":
    unittest.main()
