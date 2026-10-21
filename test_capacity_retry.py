from datetime import datetime, timezone
from email.message import Message
import io
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from packet_inspector.capacity_retry import (CAPACITY_MESSAGE, MAX_PROBE_BYTES, MAX_RETRIES,
                                             capacity_error, open_response, retry_delay)


def frame(kind, **data):
    return b"data: " + json.dumps({"type": kind, **data}).encode() + b"\n\n"


FAILED = frame("response.failed", response={"error": {"code": "server_is_overloaded"}})
SUCCESS = frame("response.output_text.delta", delta="Success") + frame("response.completed")
CREATED = frame("response.created", response={"id": "failed-attempt"})


class Response:
    def __init__(self, data, status=200, content_type="text/event-stream", extra=None, chunk=65536):
        self.data = io.BytesIO(data)
        self.status, self.reason = status, "Test response"
        self.headers = Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        for key, value in (extra or {}).items():
            self.headers[key] = value
        self.chunk = chunk
        self.closed = False

    def read1(self, size=65536):
        return self.data.read(min(size, self.chunk))

    def read(self, size=-1):
        return self.data.read(size)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CapacityRetryTests(unittest.TestCase):
    def request(self):
        return Request("https://example.invalid/responses", data=b'{"model":"original","input":[]}',
                       headers={"Authorization": "Bearer test", "Idempotency-Key": "unchanged"})

    def run_response(self, *responses, **options):
        opener = Opener(*responses)
        waits, retries = [], []
        result = open_response(opener, self.request(), enabled=options.pop("enabled", True),
                               wait=options.pop("wait", lambda delay, _: waits.append(delay)),
                               on_retry=lambda count, delay: retries.append(count), **options)
        data = result.read()
        result.close()
        return data, opener, waits, retries

    def test_disabled_does_not_inspect_or_retry(self):
        response = Response(FAILED)
        opener = Opener(response)
        result = open_response(opener, self.request())
        self.assertIs(result, response)
        self.assertEqual(response.data.tell(), 0)
        self.assertEqual(len(opener.requests), 1)

    def test_200_sse_failure_retries_and_drops_failed_handshake(self):
        failed = Response(CREATED + FAILED)
        data, opener, waits, retries = self.run_response(failed, Response(SUCCESS))
        self.assertEqual(data, SUCCESS)
        self.assertTrue(failed.closed)
        self.assertEqual(retries, [1])
        self.assertEqual(len(waits), 1)
        self.assertIs(opener.requests[0], opener.requests[1])
        self.assertEqual(opener.requests[1].data, b'{"model":"original","input":[]}')

    def test_503_http_error_preserves_retry_after_and_credentials(self):
        headers = Message()
        headers["Content-Type"], headers["Retry-After"] = "application/json", "0"
        failure = HTTPError("https://example.invalid/responses", 503, "Unavailable", headers,
                            io.BytesIO(b'{"error":{"code":"server_is_overloaded"}}'))
        data, opener, waits, _ = self.run_response(failure, Response(SUCCESS))
        self.assertEqual(data, SUCCESS)
        self.assertEqual(waits, [0])
        self.assertEqual(opener.requests[1].get_header("Authorization"), "Bearer test")
        self.assertEqual(opener.requests[1].get_header("Idempotency-key"), "unchanged")

    def test_exact_capacity_message_is_recognized_without_code(self):
        payload = json.dumps({"error": {"message": CAPACITY_MESSAGE}}).encode()
        data, opener, _, _ = self.run_response(Response(payload, content_type="application/json"), Response(SUCCESS))
        self.assertEqual((data, len(opener.requests)), (SUCCESS, 2))

    def test_retry_limit_returns_original_final_error(self):
        data, opener, _, retries = self.run_response(*(Response(FAILED) for _ in range(MAX_RETRIES + 1)))
        self.assertEqual(data, FAILED)
        self.assertEqual(len(opener.requests), MAX_RETRIES + 1)
        self.assertEqual(retries, [1, 2, 3])

    def test_other_errors_are_not_retried(self):
        for status, code in [(429, "rate_limit_exceeded"), (503, "maintenance"), (200, "invalid_prompt")]:
            with self.subTest(status=status, code=code):
                payload = json.dumps({"error": {"code": code}}).encode()
                data, opener, waits, _ = self.run_response(Response(payload, status, "application/json"))
                self.assertEqual((data, len(opener.requests), waits), (payload, 1, []))

    def test_authentication_errors_are_not_retried(self):
        data = b'{"error":{"code":"server_is_overloaded"}}'
        result, opener, _, _ = self.run_response(Response(data, 401, "application/json"))
        self.assertEqual((result, len(opener.requests)), (data, 1))

    def test_first_output_commits_stream_without_waiting_for_termination(self):
        for kind in ["response.output_text.delta", "response.function_call_arguments.delta",
                     "response.output_item.added", "response.reasoning_summary_text.delta"]:
            with self.subTest(kind=kind):
                payload = CREATED + frame(kind, delta="already emitted") + FAILED
                data, opener, waits, _ = self.run_response(Response(payload, chunk=len(CREATED) + 1))
                self.assertEqual((data, len(opener.requests), waits), (payload, 1, []))

    def test_final_failure_with_existing_output_is_not_retried(self):
        payload = frame("response.failed", response={"error": {"code": "server_is_overloaded"},
                                                     "output": [{"type": "function_call"}]})
        data, opener, _, _ = self.run_response(Response(payload))
        self.assertEqual((data, len(opener.requests)), (payload, 1))

    def test_capacity_text_in_user_data_does_not_trigger_retry(self):
        self.assertFalse(capacity_error({"input": [{"content": CAPACITY_MESSAGE}]}))
        self.assertFalse(capacity_error({"error": {"code": {}}}))
        payload = frame("response.output_text.delta", delta=CAPACITY_MESSAGE)
        data, opener, _, _ = self.run_response(Response(payload))
        self.assertEqual((data, len(opener.requests)), (payload, 1))

    def test_split_crlf_events_and_missing_content_type(self):
        payload = (CREATED + FAILED).replace(b"\n", b"\r\n")
        data, opener, _, _ = self.run_response(Response(payload, content_type=None, chunk=3), Response(SUCCESS))
        self.assertEqual((data, len(opener.requests)), (SUCCESS, 2))

    def test_error_event_name_without_json_type_is_recognized(self):
        payload = b'event: error\ndata: {"code":"server_is_overloaded"}\n\n'
        data, opener, _, _ = self.run_response(Response(payload), Response(SUCCESS))
        self.assertEqual((data, len(opener.requests)), (SUCCESS, 2))

    def test_large_handshake_is_bounded_and_forwarded_unchanged(self):
        for size in [300000, MAX_PROBE_BYTES + 1000]:
            with self.subTest(size=size):
                prefix = frame("response.created", response={"metadata": "x" * size})
                payload = prefix + FAILED
                responses = [Response(payload)]
                if size < MAX_PROBE_BYTES:
                    responses.append(Response(SUCCESS))
                data, opener, _, _ = self.run_response(*responses)
                self.assertEqual(data, SUCCESS if size < MAX_PROBE_BYTES else payload)
                self.assertEqual(len(opener.requests), 2 if size < MAX_PROBE_BYTES else 1)

    def test_unknown_and_malformed_streams_are_preserved(self):
        for data in [b"data: [DONE]\n\n", b"data: bad-json\n\n", b"raw bytes",
                     frame("unknown.event"), b""]:
            with self.subTest(data=data[:30]):
                result, opener, waits, _ = self.run_response(Response(data))
                self.assertEqual((result, len(opener.requests), waits), (data, 1, []))

    def test_encoded_responses_are_passed_through(self):
        data, opener, _, _ = self.run_response(Response(FAILED, extra={"Content-Encoding": "gzip"}))
        self.assertEqual((data, len(opener.requests)), (FAILED, 1))

    def test_retry_after_beyond_budget_does_not_retry_early(self):
        data, opener, waits, _ = self.run_response(Response(FAILED, extra={"Retry-After": "300"}))
        self.assertEqual((data, len(opener.requests), waits), (FAILED, 1, []))

    def test_disabling_during_wait_preserves_original_failure(self):
        enabled = [True]
        def wait(*_):
            enabled[0] = False
        data, opener, _, _ = self.run_response(Response(CREATED + FAILED),
                                             still_enabled=lambda: enabled[0], wait=wait)
        self.assertEqual((data, len(opener.requests)), (CREATED + FAILED, 1))

    def test_client_disconnect_closes_upstream_and_stops_retries(self):
        failed = Response(FAILED)
        opener = Opener(failed)
        def wait(*_):
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            open_response(opener, self.request(), enabled=True, wait=wait)
        self.assertTrue(failed.closed)
        self.assertEqual(len(opener.requests), 1)

    def test_retry_after_http_date_and_invalid_values(self):
        now = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_delay({"Retry-After": "Wed, 21 Oct 2026 06:00:10 GMT"}, 1, now), 10)
        for value in ["invalid", "-1", "NaN", "inf"]:
            delay = retry_delay({"Retry-After": value}, 2, now)
            self.assertGreaterEqual(delay, 4)
            self.assertLessEqual(delay, 5)


if __name__ == "__main__":
    unittest.main()
