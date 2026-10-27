"""Native retry advice must preserve streaming and never replay upstream requests."""
from email.utils import formatdate
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.native_retry import NativeRetryStream, prepare_http_error
from tests.response_fixtures import CREATED, FAILED, SUCCESS, Response, frame


class NativeRetryTests(unittest.TestCase):
    def stream(self, body, *, enabled=True, delay=7, **response_options):
        records = []
        response = Response(body, **response_options)
        stream = NativeRetryStream(response, lambda **v: records.append(v),
                                   policy=lambda: (enabled, delay))
        return b"".join(stream.chunks()), records

    def test_success_bytes_and_usage_remain_authentic(self):
        body = CREATED + frame("response.output_text.delta", delta="hello") + SUCCESS
        for chunk in (1, 17, 65536):
            result, records = self.stream(body, chunk=chunk)
            self.assertEqual(result, body)
            self.assertFalse(records)

    def test_error_after_text_reasoning_or_tools_receives_advice_without_replay(self):
        for prefix in (frame("response.output_text.delta", delta="partial"),
                       frame("response.output_item.added", item={"type":"reasoning", "encrypted_content":"opaque"}),
                       frame("response.output_item.done", item={"type":"function_call", "name":"tool", "arguments":"{}"})):
            for chunk in (1, 19, 65536):
                result, records = self.stream(CREATED + prefix + FAILED, chunk=chunk)
                self.assertTrue(result.startswith(CREATED + prefix))
                value = json.loads(result[len(CREATED + prefix):].strip().removeprefix(b"data: "))
                self.assertEqual(value["response"]["error"]["headers"]["Retry-After"], "7")
                self.assertEqual(records, [{"native_retry_advised":True, "native_retry_delay_ms":7000}])

    def test_visible_output_is_yielded_before_reading_the_failure(self):
        text = frame("response.output_text.delta", delta="show immediately")
        class GatedResponse(Response):
            def read1(self, size=65536):
                if self.data.tell() >= len(text) and not released:
                    raise AssertionError("read later events before forwarding visible output")
                return super().read1(size)
        released = False
        response = GatedResponse(text + FAILED, chunk=len(text))
        chunks = NativeRetryStream(response, lambda **_: None, policy=lambda: (True, 1)).chunks()
        self.assertEqual(next(chunks), text)
        released = True
        self.assertIn(b'"Retry-After":"1"', b"".join(chunks))

    def test_time_and_enabled_setting_are_read_at_each_error(self):
        policy = [True, 2]
        stream = NativeRetryStream(Response(FAILED + FAILED, chunk=len(FAILED)), lambda **_: None,
                                   policy=lambda: policy)
        chunks = stream.chunks()
        self.assertIn(b'"Retry-After":"2"', next(chunks))
        policy[:] = [False, 9]
        self.assertEqual(b"".join(chunks), FAILED)

    def test_bare_capacity_error_is_adapted_to_native_failed_response(self):
        for body in (frame("error", code="server_is_overloaded"),
                     frame("error", error={"code":"server_is_overloaded"})):
            result, _ = self.stream(body)
            payload = json.loads(result.strip().removeprefix(b"data: "))
            self.assertEqual(payload["type"], "response.failed")
            self.assertEqual(payload["response"]["error"]["headers"]["Retry-After"], "7")

    def test_oversize_unknown_error_does_not_block_later_capacity_advice(self):
        from packet_inspector.usage_trigger import MAX_EVENT_BYTES
        first = frame("response.output_text.delta", delta="a"*(MAX_EVENT_BYTES+10))
        result, _ = self.stream(first + FAILED)
        self.assertTrue(result.startswith(first))
        self.assertIn(b'"Retry-After":"7"', result[len(first):])

    def test_longer_upstream_advice_is_preserved_including_http_dates(self):
        body = frame("response.failed", response={"error":{"code":"server_is_overloaded",
                     "headers":{"retry-after":"12", "X-Upstream":"preserved"}}})
        result, _ = self.stream(body, delay=3, extra={"Retry-After":"20"})
        value = json.loads(result.strip().removeprefix(b"data: "))
        self.assertEqual(value["response"]["error"]["headers"], {"Retry-After":"20", "X-Upstream":"preserved"})
        result, _ = self.stream(FAILED, extra={"Retry-After":formatdate(time.time()+120, usegmt=True)})
        value = json.loads(result.strip().removeprefix(b"data: "))
        self.assertGreaterEqual(int(value["response"]["error"]["headers"]["Retry-After"]), 119)

    def test_disabled_unknown_noncapacity_or_encoded_content_is_unchanged(self):
        cases = [frame("response.output_text.delta", delta="Selected model is at capacity. Please try a different model."),
                 frame("response.failed", response={"error":{"code":"invalid_prompt"}}),
                 b"data: invalid json\n\n", frame("response.future_event", error={"code":"server_is_overloaded"})]
        for body in cases:
            self.assertEqual(self.stream(body)[0], body)
        self.assertEqual(self.stream(FAILED, enabled=False)[0], FAILED)
        self.assertEqual(self.stream(FAILED, extra={"Content-Encoding":"gzip"})[0], FAILED)

    def test_http_capacity_errors_receive_outer_and_nested_advice(self):
        original = b'{"error":{"code":"server_is_overloaded","message":"capacity"}}'
        for status in (429, 500, 502, 503, 504):
            records = []
            response = Response(original, status=status, content_type="application/json",
                                extra={"Content-Length":str(len(original)), "ETag":"stale"})
            prepared = prepare_http_error(response, lambda: (True, 9), lambda **v: records.append(v))
            result = prepared.read()
            self.assertEqual(prepared.headers["Retry-After"], "9")
            self.assertEqual(json.loads(result)["error"]["headers"]["Retry-After"], "9")
            self.assertEqual(int(prepared.headers["Content-Length"]), len(result))
            self.assertNotIn("ETag", prepared.headers)
            self.assertTrue(records[0]["native_retry_advised"])
            prepared.close()
            self.assertTrue(response.closed)

    def test_forbidden_payload_and_unknown_http_errors_are_not_reclassified(self):
        body = b'{"error":{"code":"server_is_overloaded"}}'
        for status in (401, 403, 413):
            response = Response(body, status=status, content_type="application/json")
            self.assertIs(prepare_http_error(response, lambda: (True, 1), lambda **_: None), response)
        response = Response(b'{"error":{"code":"other"}}', status=503, content_type="application/json")
        prepared = prepare_http_error(response, lambda: (True, 1), lambda **_: None)
        self.assertEqual(prepared.read(), b'{"error":{"code":"other"}}')
        self.assertNotIn("Retry-After", prepared.headers)


class NativeRetryConfigurationTests(unittest.TestCase):
    def setUp(self):
        from packet_inspector.server import InspectorServer
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.config = root/"config.toml"
        self.config.write_text('model_provider="probe"\n[model_providers.probe]\n'
                               'base_url="http://127.0.0.1:9/v1"\nstream_max_retries=8\nrequest_max_retries=2\n')
        with patch("packet_inspector.server.startup_supported", return_value=False):
            self.server = InspectorServer(("127.0.0.1", 0), root/"data", self.config, "http://127.0.0.1:9/v1")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.server.set_connection(True)

    def settings(self, values):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            connection.request("POST", "/api/settings", json.dumps(values).encode())
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def provider(self):
        return tomllib.loads(self.config.read_text())["model_providers"]["probe"]

    def test_new_install_defaults_are_fifteen_retries_and_thirty_seconds(self):
        self.assertEqual(self.server.settings["capacity_retry_max_retries"], 15)
        self.assertEqual(self.server.settings["capacity_retry_delay_seconds"], 30)
        self.assertFalse(self.server.settings["capacity_retry_enabled"])

    def test_settings_control_native_budget_and_live_delay_then_restore_original(self):
        status, result = self.settings({"capacity_retry_enabled":True, "capacity_retry_max_retries":12,
                                        "capacity_retry_delay_seconds":9})
        self.assertEqual(status, 200)
        self.assertEqual(result["capacity_retry_mode"], "native")
        self.assertEqual((self.provider()["stream_max_retries"], self.provider()["request_max_retries"]), (12, 0))
        before = self.config.read_bytes()
        self.assertEqual(self.settings({"capacity_retry_delay_seconds":4})[0], 200)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.server.settings["capacity_retry_delay_seconds"], 4)
        self.assertEqual(self.settings({"capacity_retry_enabled":False})[0], 200)
        self.assertEqual((self.provider()["stream_max_retries"], self.provider()["request_max_retries"]), (8, 2))

    def test_later_settings_failure_rolls_back_native_retry_configuration(self):
        with patch("packet_inspector.server.startup_supported", return_value=True), patch(
                "packet_inspector.server.configure_startup", side_effect=OSError("synthetic startup failure")):
            status, _ = self.settings({"capacity_retry_enabled":True, "capacity_retry_max_retries":12, "start_on_login":True})
        self.assertEqual(status, 500)
        self.assertFalse(self.server.settings["capacity_retry_enabled"])
        self.assertEqual((self.provider()["stream_max_retries"], self.provider()["request_max_retries"]), (8, 2))

    def test_retry_count_and_delay_accept_both_ends_of_user_range(self):
        for value in (1, 99):
            status, result = self.settings({"capacity_retry_enabled":True, "capacity_retry_max_retries":value,
                                            "capacity_retry_delay_seconds":value})
            self.assertEqual(status, 200)
            self.assertEqual(result["capacity_retry_delay_seconds"], value)
            self.assertEqual(self.provider()["stream_max_retries"], value)


if __name__ == "__main__":
    unittest.main()
