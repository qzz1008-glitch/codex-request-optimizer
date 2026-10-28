"""Diagnostics are event-triggered and never change network response bytes."""
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from packet_inspector.network_diagnostics import (
    DiagnosticHTTPHandler, DiagnosticHTTPSHandler, NetworkDiagnostics, error_body,
    environment_snapshot, resolve_probe, safe_url)
from packet_inspector.server import Handler, InspectorServer
from packet_inspector.overflow import OverflowFailure
from packet_inspector.storage_paths import resolve_directory


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        self.server.calls.append(("CONNECT", self.path, b""))
        self.send_response(self.server.connect_status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls.append((self.command, self.path, body))
        if self.path.startswith("/reset"):
            self.close_connection = True
            return
        if self.path.startswith("/html"):
            status, kind, response = 504, "text/html", self.server.html
        elif self.path.startswith("/stream-error"):
            status, kind, response = 200, "text/event-stream", self.server.stream_error
        elif self.path.startswith("/bad-json"):
            status, kind, response = 403, "application/json", b'{"error":{"code":"blocked","message":"blocked at proxy"}}'
        else:
            status, kind, response = 200, "text/event-stream", self.server.success
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(response)))
        self.send_header("X-Request-ID", "gateway-trace-123")
        self.send_header("Set-Cookie", "must-not-be-saved")
        self.end_headers()
        self.wfile.write(response)


class NetworkRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.gateway.calls = []
        self.gateway.connect_status = 504
        self.gateway.html = (b'<!doctype html><html><head><title>HIS Proxy Notification</title>'
                             b'<style>hidden-css-marker</style></head><body>'
                             b'Upstream connect timed out. Error code: TEST504</body></html>')
        self.gateway.stream_error = b'data: {"type":"response.failed","response":{"error":{"code":"upstream_busy","message":"capacity test reason"}}}\n\n'
        self.gateway.success = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":42,"output_tokens":3}}}\n\n'
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.upstream = "http://127.0.0.1:" + str(self.gateway.server_port)
        self.config = self.root / "config.toml"
        self.config.write_text('model_provider="test"\n[model_providers.test]\nname="Test"\nwire_api="responses"\n'
                               'base_url=' + json.dumps(self.upstream) + '\n', encoding="utf-8")
        self.server = InspectorServer(("127.0.0.1", 0), self.root / "data", self.config, self.upstream)
        self.server.settings["auto_detect_providers"] = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = build_opener(ProxyHandler({}))

    def send(self, route, body=b'{"model":"test","input":[]}'):
        self.server.settings["upstream"] = self.upstream + route
        request = Request(self.server.endpoint + "/responses", data=body,
                          headers={"Content-Type": "application/json", "Authorization": "Bearer secret-test-token"})
        try:
            response = self.client.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            result = response.status, response.read()
        # The server finalizes diagnostics after its last socket write.
        end = time.monotonic() + 2
        while self.server.active_requests and time.monotonic() < end:
            time.sleep(.01)
        return result

    def records(self):
        return self.server.network_diagnostics.export()["records"]

    def test_no_startup_sync_or_success_diagnostic(self):
        self.assertFalse(self.server.network_diagnostics.path.exists())
        self.assertEqual(self.send("/ok"), (200, self.gateway.success))
        self.assertEqual(self.records(), [])
        self.assertFalse(self.server.network_diagnostics.path.exists())
        self.server.settings["auto_detect_providers"] = True
        self.server.sync_providers()
        self.assertFalse(self.server.network_diagnostics.path.exists())

    def test_html_504_preserves_response_and_records_gateway_reason_and_flow(self):
        body = b'{"model":"test","input":[],"marker":"not-a-diagnostic-payload"}'
        self.assertEqual(self.send("/html", body), (504, self.gateway.html))
        record, = self.records()
        self.assertEqual(record["status"], 504)
        self.assertIn("TEST504", record["error_body"]["text"])
        self.assertNotIn("hidden-css-marker", record["error_body"]["text"])
        self.assertEqual(record["response_headers"]["x-request-id"], "gateway-trace-123")
        self.assertEqual(record["socket_endpoint"]["port"], self.gateway.server_port)
        self.assertEqual(record["peer_ip"], "127.0.0.1")
        self.assertEqual(record["flow"]["uploaded_body_bytes"], len(body))
        self.assertEqual(record["flow"]["response_body_bytes"], len(self.gateway.html))
        self.assertIn("response_header_wait", record["timings_ms"])
        text = json.dumps(record)
        for forbidden in ("secret-test-token", "must-not-be-saved", "not-a-diagnostic-payload"):
            self.assertNotIn(forbidden, text)

    def test_error_stream_is_recorded_once_and_success_output_is_not_saved(self):
        self.assertEqual(self.send("/stream-error"), (200, self.gateway.stream_error))
        record, = self.records()
        self.assertIn("capacity test reason", record["stream_error"])
        self.assertEqual(record["stream_event"], "response.failed")

    def test_local_protection_error_records_that_request_was_not_sent(self):
        self.server.settings["packet_size_protection_enabled"] = True
        with patch.object(self.server.overflow, "prepare", side_effect=OverflowFailure("test_rejected", "local test reason", 413)):
            self.assertEqual(self.send("/ok")[0], 413)
        self.assertEqual(self.gateway.calls, [])
        record, = self.records()
        self.assertEqual(record["kind"], "local_request_error")
        self.assertFalse(record["request_sent"])
        self.assertIn("local test reason", record["exception"][0]["message"])

    def test_compaction_error_logs_prepared_headers_and_preserves_failure_phase(self):
        handler = Handler.__new__(Handler)
        handler.server = self.server
        with self.assertRaises(OverflowFailure):
            handler._checkpoint(self.upstream + "/html", "/responses",
                                {"Content-Encoding": "zstd", "Accept": "application/json"}, b"{}", "identity")
        record, = self.records()
        self.assertEqual(record["request_kind"], "relay_compaction")
        self.assertEqual(record["failure_phase"], "upstream_http_response")
        self.assertEqual(record["request_headers"]["content-encoding"], "identity")
        self.assertEqual(record["request_headers"]["accept"], "text/event-stream")
        self.assertIn("TEST504", record["error_body"]["text"])

    def test_connection_reset_has_a_failure_phase(self):
        with self.assertRaises(http.client.RemoteDisconnected):
            self.send("/reset")
        end = time.monotonic() + 2
        while not self.records() and time.monotonic() < end:
            time.sleep(.01)
        record, = self.records()
        self.assertEqual(record["failure_phase"], "response_headers")
        self.assertTrue(record["exception"])

    def test_connect_button_is_the_only_probe_trigger(self):
        ready = threading.Event()
        with patch.object(NetworkDiagnostics, "_probe", side_effect=lambda record: ready.set()) as probe:
            self.server.set_connection(True)
            self.assertTrue(ready.wait(1))
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["trigger"], "user_connect")
            self.server.set_connection(False)
            self.server.auto_sync_providers()
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(len(self.records()), 1)

    def test_export_is_a_download_and_does_not_trigger_probe(self):
        self.send("/html")
        with patch.object(NetworkDiagnostics, "_probe") as probe:
            with self.client.open(self.server.ui_url.rstrip("/") + "/api/network-diagnostics?download=1") as response:
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertEqual(json.load(response)["records"][0]["status"], 504)
            probe.assert_not_called()

    def test_saved_directories_apply_on_next_launch_without_switching_active_requests(self):
        cache, logs = self.root / "new cache", self.root / "new logs"
        request = Request(self.server.ui_url.rstrip("/") + "/api/settings",
                          data=json.dumps({"cache_dir": str(cache), "log_dir": str(logs)}).encode(),
                          headers={"Content-Type": "application/json", "Origin": self.server.ui_url.rstrip("/")})
        with self.client.open(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.server.store.path.parent, self.root / "data")
        self.assertEqual(self.server.network_diagnostics.path.parent, self.root / "data")
        self.assertTrue(self.server.status()["storage_paths_pending"])
        self.send("/html")
        self.assertTrue(self.server.network_diagnostics.path.exists())
        with InspectorServer(("127.0.0.1", 0), self.root / "data", self.config, self.upstream) as replacement:
            self.assertEqual(replacement.store.path.parent, cache)
            self.assertEqual(replacement.overflow.path.parent, cache)
            self.assertEqual(replacement.retry_journal.path.parent, logs)
            self.assertEqual(replacement.network_diagnostics.path.parent, logs)
            self.assertFalse(replacement.status()["storage_paths_pending"])
            self.assertFalse(replacement.network_diagnostics.path.exists())

    def test_invalid_directory_does_not_save_any_settings(self):
        previous = dict(self.server.settings)
        request = Request(self.server.ui_url.rstrip("/") + "/api/settings",
                          data=json.dumps({"log_dir": "relative", "cache_dir": str(self.root / "valid")}).encode(),
                          headers={"Content-Type": "application/json", "Origin": self.server.ui_url.rstrip("/")})
        with self.assertRaises(HTTPError) as error:
            self.client.open(request)
        error.exception.close()
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server.settings, previous)

    def test_session_summary_uses_active_cache_directory(self):
        with patch.object(self.server.session_summaries, "read", return_value={}) as read:
            with self.client.open(self.server.ui_url.rstrip("/") + "/api/session-summaries"):
                pass
            read.assert_called_once_with(self.server.cache_dir)

    def test_failed_connect_configuration_is_logged_without_probe(self):
        with patch.object(self.server, "sync_providers", side_effect=ValueError("configuration test error")), \
                patch.object(NetworkDiagnostics, "_probe") as probe:
            with self.assertRaises(ValueError):
                self.server.set_connection(True)
            record, = self.records()
            self.assertEqual(record["outcome"], "failed")
            self.assertIn("configuration test error", record["exception"][0]["message"])
            probe.assert_not_called()

    def test_proxy_connect_failure_and_tls_failure_are_distinguishable(self):
        proxy = self.upstream
        for connect_status, expected in ((504, "proxy_connect"), (200, "tls")):
            with self.subTest(connect_status=connect_status):
                self.gateway.connect_status = connect_status
                journal = NetworkDiagnostics(self.root / "data", {"https": proxy})
                opener = build_opener(ProxyHandler(journal.proxies), DiagnosticHTTPHandler(), DiagnosticHTTPSHandler())
                trace = journal.request("https://upstream.invalid/responses", "POST", {}, b"{}")
                with patch("urllib.request.proxy_bypass", return_value=False):
                    try:
                        with trace.active():
                            opener.open(Request("https://upstream.invalid/responses", data=b"{}"), timeout=2)
                    except (OSError, URLError) as exc:
                        trace.finish(exc)
                    else:
                        self.fail("Synthetic proxy must fail before contacting an upstream")
                record = next(r for r in journal.export()["records"] if r["id"] == trace.record["id"])
                self.assertEqual(record["failure_phase"], expected)
                self.assertEqual(record["socket_endpoint"]["port"], self.gateway.server_port)
                self.assertEqual(record["socket_endpoint"]["tunnel_target"], "upstream.invalid")


class DiagnosticDataTests(unittest.TestCase):
    def test_stream_errors_do_not_capture_following_output(self):
        with tempfile.TemporaryDirectory() as td:
            journal = NetworkDiagnostics(Path(td), {})
            trace = journal.request("https://upstream.invalid/responses", "POST", {}, b"{}")
            response = type("Response", (), {"status": 200, "headers": {"Content-Type": "text/event-stream"}})()
            trace.response(response)
            trace.stream_error({"type": "error", "message": "test failure"})
            trace.feed(b'data: {"type":"response.output_text.delta","delta":"private conversation canary"}\n\n')
            trace.finish()
            self.assertNotIn("private conversation canary", json.dumps(journal.export()))

    def test_error_pages_are_decoded_bounded_and_redacted(self):
        raw = b'<html><script>not-kept</script><body>Proxy error\nAuthorization: Bearer actual-secret\nhttps://user:password@host.invalid/path?token=secret</body></html>'
        result = error_body(gzip.compress(raw), {"content-type": "text/html", "content-encoding": "gzip"}, ["actual-secret"])
        text = json.dumps(result)
        self.assertIn("Proxy error", text)
        for secret in ("actual-secret", "password", "token=secret", "not-kept"):
            self.assertNotIn(secret, text)
        result = error_body(b"x" * 200000, {"content-type": "text/plain"})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"]), 24000)

    def test_environment_reports_proxy_source_and_pac_without_credentials(self):
        env = {"https": "http://user:secret@region.example:8080"}
        with patch("urllib.request.getproxies_environment", return_value=env), \
                patch("urllib.request.getproxies", return_value=env), \
                patch("urllib.request.getproxies_registry", return_value={}, create=True), \
                patch("packet_inspector.network_diagnostics.windows_proxy_settings", return_value={"AutoConfigURL": "http://pac.example/script"}):
            report = environment_snapshot(env)
        self.assertEqual(report["proxy_configuration_source_now"], "environment")
        self.assertEqual(report["opener_proxies"]["https"], "http://region.example:8080")
        self.assertFalse(report["pac_supported_by_transport"])
        self.assertNotIn("secret", json.dumps(report))

    def test_retention_and_record_limits(self):
        with tempfile.TemporaryDirectory() as td:
            journal = NetworkDiagnostics(Path(td), {})
            with patch("packet_inspector.network_diagnostics.MAX_RECORDS", 2), \
                    patch("packet_inspector.network_diagnostics.MAX_BYTES", 500):
                for i in range(5):
                    journal._save({"id": str(i), "created": time.time() + i, "kind": "test"})
                journal._save({"id": "expired", "created": 1, "kind": "test"})
            records = journal.export()["records"]
            self.assertEqual({r["id"] for r in records}, {"3", "4"})

    def test_diagnostic_write_failure_cannot_break_a_response(self):
        with tempfile.TemporaryDirectory() as td:
            journal = NetworkDiagnostics(Path(td), {})
            trace = journal.request("https://upstream.invalid/responses", "POST", {}, b"{}")
            response = type("Response", (), {"status": 504, "headers": {"Content-Type": "text/html"}})()
            trace.response(response)
            trace.feed(b"<html>timeout</html>")
            with patch("packet_inspector.network_diagnostics.sqlite3.connect", side_effect=OSError("disk unavailable")):
                trace.finish()
            self.assertEqual(journal.errors, 1)

    def test_dns_probe_has_a_deadline(self):
        release = threading.Event()
        with patch("packet_inspector.network_diagnostics.socket.getaddrinfo", side_effect=lambda *a, **k: release.wait(1)):
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    resolve_probe("test.invalid", 443, .02)
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()

    def test_paths_reject_relative_and_file_and_urls_hide_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaises(ValueError):
                resolve_directory(root, "relative/path")
            file = root / "file"; file.write_text("content")
            with self.assertRaises(ValueError):
                resolve_directory(root, str(file))
            self.assertEqual(resolve_directory(root, ""), root)
        self.assertNotIn("secret", safe_url("http://user:secret@localhost/relay/secret/p/test?key=secret"))
