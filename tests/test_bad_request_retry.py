"""A generic rejected request can recover without exposing its first HTTP 400."""
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from packet_inspector.bad_request_retry import MAX_ERROR_BYTES, inspect_bad_request, retry_bad_request
from packet_inspector.http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from tests import test_packet_inspector as fixtures
from tests.response_fixtures import FAILED, SUCCESS, Opener, Response

BAD_REQUEST = b'{"detail": "Bad Request"}'


class BadRequestRetryTests(unittest.TestCase):
    def run_retry(self, *responses, max_retries=1, enabled=None, started=0, wait=None):
        now = [0.0]
        records, events, rejected, timeouts = [], [], [], []
        opener = Opener(*responses[1:])
        def open_retry(retry, timeout):
            timeouts.append(timeout)
            return opener.open(None, timeout=timeout)
        def sleep(seconds):
            now[0] += seconds
            if wait:
                wait(seconds, now)
        result = retry_bad_request(responses[0], open_retry, max_retries=max_retries, started=started,
            enabled=enabled or (lambda: True), before_retry=rejected.append,
            record=lambda **fields: records.append(fields),
            on_event=lambda event, **fields: events.append((event, fields)), clock=lambda: now[0], wait=sleep)
        return result, opener, records, events, rejected, timeouts

    def test_generic_error_recovers_and_closes_only_the_rejected_response(self):
        failed = Response(BAD_REQUEST, status=400, content_type="application/json")
        success = Response(SUCCESS)
        restored = []
        success.inspector_set_read_timeout = restored.append
        result, opener, records, events, rejected, timeouts = self.run_retry(failed, success)
        self.assertIs(result, success)
        self.assertEqual(result.read(), SUCCESS)
        self.assertTrue(failed.closed)
        self.assertFalse(success.closed)
        self.assertEqual(records, [{"bad_request_retry_count": 1}])
        self.assertEqual(rejected, [BAD_REQUEST])
        self.assertEqual(len(opener.requests), 1)
        self.assertLessEqual(timeouts[0], 60)
        self.assertEqual(restored, [UPSTREAM_READ_TIMEOUT_SECONDS])

    def test_exhaustion_preserves_the_last_original_error(self):
        responses = [Response(BAD_REQUEST, status=400, content_type="application/json") for _ in range(3)]
        result, opener, records, events, rejected, _ = self.run_retry(*responses, max_retries=2)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.read(), BAD_REQUEST)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(records[-1], {"bad_request_retry_count": 2})
        self.assertEqual(events[-1][1]["reason"], "retry_limit")
        result.close()
        self.assertTrue(all(response.closed for response in responses))

    def test_other_errors_and_malformed_or_detailed_400s_are_untouched(self):
        cases = [(400, b'{"detail":"Bad Request","param":"input"}', "application/json", {}),
                 (400, b'{"detail":[{"msg":"Bad Request"}]}', "application/json", {}),
                 (400, b'{"error":{"message":"Bad Request","type":"invalid_request_error"}}', "application/json", {}),
                 (400, b'{"detail":"bad field","detail":"Bad Request"}', "application/json", {}),
                 (400, b'{"detail":', "application/json", {}),
                 (400, BAD_REQUEST, "text/html", {}),
                 (400, BAD_REQUEST, "application/json", {"Content-Encoding":"gzip"}),
                 *[(status, BAD_REQUEST, "application/json", {}) for status in (200, 403, 413, 429, 500, 502, 503, 504)]]
        for status, body, kind, extra in cases:
            with self.subTest(status=status, body=body, content_type=kind, extra=extra):
                response = Response(body, status=status, content_type=kind, extra=extra)
                prepared, opener, records, _, rejected, _ = self.run_retry(response)
                self.assertEqual(prepared.status, status)
                self.assertEqual(prepared.read(), body)
                self.assertFalse(opener.requests)
                self.assertFalse(records)
                self.assertFalse(rejected)

    def test_large_errors_are_bounded_and_preserve_the_entire_body(self):
        raw = b'{"detail":"' + b'x' * (MAX_ERROR_BYTES * 2) + b'"}'
        for extra in ({}, {"Content-Length": str(len(raw))}):
            response = Response(raw, status=400, content_type="application/json", extra=extra)
            prepared, rejected = inspect_bad_request(response)
            self.assertIsNone(rejected)
            self.assertLessEqual(response.data.tell(), MAX_ERROR_BYTES + 1)
            self.assertEqual(prepared.read(), raw)

    def test_disabling_during_wait_returns_the_original_error_without_replay(self):
        enabled = [True]
        response = Response(BAD_REQUEST, status=400, content_type="application/json")
        result, opener, records, events, _, _ = self.run_retry(response, enabled=lambda: enabled[0],
            wait=lambda *_: enabled.__setitem__(0, False))
        self.assertEqual(result.read(), BAD_REQUEST)
        self.assertFalse(opener.requests)
        self.assertFalse(records)
        self.assertEqual(events[-1][1]["reason"], "disabled")

    def test_time_budget_and_upstream_retry_after_prevent_late_attempts(self):
        for started, extra, wait in ((-UPSTREAM_READ_TIMEOUT_SECONDS, {}, None),
                                    (0, {"Retry-After":"120"}, None),
                                    (0, {}, lambda _, now: now.__setitem__(0, 61))):
            response = Response(BAD_REQUEST, status=400, content_type="application/json", extra=extra)
            result, opener, records, events, _, _ = self.run_retry(response, started=started, wait=wait)
            self.assertEqual(result.read(), BAD_REQUEST)
            self.assertFalse(opener.requests)
            self.assertFalse(records)
            self.assertEqual(events[-1][1]["reason"], "time_budget")

    def test_exception_during_wait_closes_the_response_and_does_not_replay(self):
        response = Response(BAD_REQUEST, status=400, content_type="application/json")
        def interrupted(*_):
            raise ConnectionResetError()
        with self.assertRaises(ConnectionResetError):
            self.run_retry(response, wait=interrupted)
        self.assertTrue(response.closed)

    def test_a_retry_transport_error_is_not_replayed_as_another_bad_request(self):
        response = Response(BAD_REQUEST, status=400, content_type="application/json")
        with self.assertRaises(TimeoutError):
            self.run_retry(response, TimeoutError("synthetic timeout"), max_retries=9)
        self.assertTrue(response.closed)


class BadRequestRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)
        cls.initial_settings = dict(cls.server.settings)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    relay = fixtures.RelayTests.relay
    api = fixtures.RelayTests.api
    latest = fixtures.RelayTests.latest

    def setUp(self):
        self.server.save_settings({**self.server.settings, "bad_request_retry_enabled": True,
            "bad_request_retry_max_retries": 1, "capacity_retry_enabled": False, "capture_enabled": True})
        fast = patch("packet_inspector.server.retry_bad_request", partial(retry_bad_request, wait=lambda _: None))
        fast.start()
        self.addCleanup(fast.stop)

    def test_new_install_stays_off_until_enabled(self):
        self.assertFalse(self.initial_settings["bad_request_retry_enabled"])
        self.assertEqual(self.initial_settings["bad_request_retry_max_retries"], 3)
        self.server.settings["bad_request_retry_enabled"] = False
        with patch.object(self.server.local_opener, "open", return_value=Response(BAD_REQUEST, status=400, content_type="application/json")) as opened:
            self.assertEqual(self.relay(b'{}'), (400, BAD_REQUEST))
        self.assertEqual(opened.call_count, 1)

    def test_recovers_before_client_headers_and_resends_identical_compressed_bytes(self):
        opener = Opener(Response(BAD_REQUEST, status=400, content_type="application/json"), Response(SUCCESS))
        raw = json.dumps({"model":"test", "input":[{"role":"user", "content":"retained content "*5000}]}).encode()
        wire = fixtures.zstandard.ZstdCompressor().compress(raw)
        with patch.object(self.server.local_opener, "open", side_effect=opener.open):
            self.assertEqual(self.relay(wire, headers={"Content-Encoding":"zstd"}), (200, SUCCESS))
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0], opener.requests[1])
        self.assertEqual(opener.requests[0].data, opener.requests[1].data)
        self.assertEqual(opener.requests[1].data, wire)
        self.assertEqual(opener.requests[0].get_header("Content-encoding"), "zstd")
        meta, _ = self.latest()
        self.assertEqual(meta["bad_request_retry_count"], 1)
        self.assertEqual(meta["capacity_retry_count"], 0)
        trace = self.server.retry_journal.get(meta["id"])
        self.assertEqual((trace["attempt_count"], trace["retry_count"], trace["bad_request_retry_count"]), (2, 1, 1))
        self.assertEqual(trace["outcome"], "success")
        self.assertLess([e["event"] for e in trace["events"]].index("bad_request_retry_finished"),
                        [e["event"] for e in trace["events"]].index("downstream_headers_sent"))
        records = [r for r in self.server.network_diagnostics.export()["records"] if r.get("packet_id") == meta["id"]]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["error_body"]["text"], BAD_REQUEST.decode())
        self.assertFalse(records[0]["downstream_headers_sent"])
        self.assertEqual(self.server.store.list(limit=1, trend=True)[0]["bad_request_retry_count"], 1)

    def test_all_attempts_are_logged_and_exhaustion_returns_the_unmodified_400(self):
        self.server.settings["bad_request_retry_max_retries"] = 2
        opener = Opener(*(Response(BAD_REQUEST, status=400, content_type="application/json") for _ in range(3)))
        with patch.object(self.server.local_opener, "open", side_effect=opener.open):
            self.assertEqual(self.relay(b'{}'), (400, BAD_REQUEST))
        self.assertEqual(len(opener.requests), 3)
        meta, _ = self.latest()
        trace = self.server.retry_journal.get(meta["id"])
        self.assertEqual(meta["bad_request_retry_count"], 2)
        self.assertEqual(trace["bad_request_retry_stop_reason"], "retry_limit")
        self.assertEqual(trace["bad_request_retry_max_retries"], 2)
        self.assertEqual(trace["outcome"], "http_error")
        self.assertEqual([e["status"] for e in trace["events"] if e["event"] == "response_headers"], [400, 400, 400])
        records = [r for r in self.server.network_diagnostics.export()["records"] if r.get("packet_id") == meta["id"]]
        self.assertEqual(len(records), 3)
        self.assertEqual(sorted(r["downstream_headers_sent"] for r in records), [False, False, True])

    def test_explicit_errors_and_non_model_endpoints_are_never_replayed(self):
        for path, raw, status in (("/responses", b'{"detail":"Bad Request","param":"input"}', 400),
                                  ("/responses", BAD_REQUEST, 403), ("/other", BAD_REQUEST, 400)):
            with self.subTest(path=path, status=status):
                with patch.object(self.server.local_opener, "open", return_value=Response(raw, status=status, content_type="application/json")) as opened:
                    self.assertEqual(self.relay(b'{}', path), (status, raw))
                self.assertEqual(opened.call_count, 1)

    def test_compact_can_recover_but_a_successful_stream_is_not_buffered(self):
        for path in ("/responses", "/responses/compact"):
            success = Response(SUCCESS)
            success.read = lambda *_: self.fail("A successful stream must not be prefetched")
            opener = Opener(Response(BAD_REQUEST, status=400, content_type="application/json"), success)
            with patch.object(self.server.local_opener, "open", side_effect=opener.open):
                self.assertEqual(self.relay(b'{}', path), (200, SUCCESS))
            self.assertEqual(len(opener.requests), 2)

    def test_capacity_advice_remains_native_after_a_generic_retry(self):
        self.server.settings.update(capacity_retry_enabled=True, capacity_retry_delay_seconds=7)
        opener = Opener(Response(BAD_REQUEST, status=400, content_type="application/json"), Response(FAILED))
        with patch.object(self.server.local_opener, "open", side_effect=opener.open):
            status, raw = self.relay(b'{}')
        self.assertEqual(status, 200)
        self.assertIn(b'"Retry-After":"7"', raw)
        self.assertEqual(len(opener.requests), 2)
        meta, _ = self.latest()
        self.assertTrue(meta["native_retry_advised"])
        self.assertEqual((meta["bad_request_retry_count"], meta["capacity_retry_count"]), (1, 0))

    def test_settings_are_persisted_live_without_changing_native_config(self):
        before = self.config.read_bytes()
        for count in (1, 9):
            status, result = self.api("/api/settings", {"bad_request_retry_enabled":True, "bad_request_retry_max_retries":count})
            self.assertEqual(status, 200)
            self.assertEqual(result["bad_request_retry_max_retries"], count)
            self.assertTrue(result["bad_request_retry_supported"])
        self.assertEqual(self.config.read_bytes(), before)
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        self.assertEqual(saved["bad_request_retry_max_retries"], 9)
        before = dict(self.server.settings)
        for count in (0, -1, 10, True, 1.5, "2", None):
            status, _ = self.api("/api/settings", {"bad_request_retry_enabled":False, "bad_request_retry_max_retries":count})
            self.assertEqual(status, 400)
            self.assertEqual(self.server.settings, before)
        self.assertEqual(self.api("/api/settings", {"bad_request_retry_enabled":"true"})[0], 400)

    def test_retry_audit_survives_paused_packet_capture(self):
        self.server.settings["capture_enabled"] = False
        opener = Opener(Response(BAD_REQUEST, status=400, content_type="application/json"), Response(SUCCESS))
        with patch.object(self.server.local_opener, "open", side_effect=opener.open):
            self.assertEqual(self.relay(b'{}'), (200, SUCCESS))
        with self.server.retry_journal._connect() as db:
            trace = json.loads(db.execute('SELECT document FROM retry_requests ORDER BY created DESC LIMIT 1').fetchone()[0])
        self.assertIsNone(trace["packet_id"])
        self.assertEqual(trace["bad_request_retry_count"], 1)
        self.assertEqual(trace["outcome"], "success")

    def test_real_http_error_then_stream_restores_the_normal_socket_idle_timeout(self):
        seen = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                status, body, kind = ((400, BAD_REQUEST, "application/json") if len(seen) == 1 else
                                      (200, SUCCESS, "text/event-stream"))
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        old = self.server.settings["upstream"]
        try:
            self.server.settings["upstream"] = f'http://127.0.0.1:{gateway.server_port}/v1'
            self.assertEqual(self.relay(b'{}'), (200, SUCCESS))
            self.assertEqual(seen, [b'{}', b'{}'])
        finally:
            self.server.settings["upstream"] = old
            gateway.shutdown()
            gateway.server_close()
            thread.join(timeout=5)


class BadRequestSettingsBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser contracts")
    def test_controls_persisted_values_and_bilingual_layout(self):
        result = subprocess.run([shutil.which("node"), "tests/bad_request_settings.cjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf8", timeout=55)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        if "skipped" in summary:
            self.skipTest(summary["skipped"])
        self.assertEqual(summary["bad_request_settings"], "passed")
        self.assertEqual(summary["browser_cases"], 5)


if __name__ == "__main__":
    unittest.main()
