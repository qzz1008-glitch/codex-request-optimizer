"""Equivalent Responses envelopes must agree across all passive consumers."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from packet_inspector.native_retry import NativeRetryStream
from packet_inspector.overflow import CheckpointStream, OverflowFailure
from packet_inspector.response_events import decode_sse, stream_event_class
from packet_inspector.response_summary import ResponseSummary
from packet_inspector.retry_diagnostics import ResponseObservation
from packet_inspector.usage_trigger import UsageStream
from tests.response_fixtures import Response


USAGE = {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130,
         "future_details": {"value": 9}}
OUTPUT = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "handoff"}]}]


def named(kind, payload, *, newline=b"\n", multiline=False):
    text = json.dumps(payload, indent=2 if multiline else None).encode()
    return (b": keepalive" + newline + b"id: preserved" + newline + b"event: " + kind.encode() + newline
            + newline.join(b"data: " + line for line in text.split(b"\n")) + newline * 2)


class ResponseCompatibilityTests(unittest.TestCase):
    def test_named_multiline_completion_is_observed_without_changing_bytes(self):
        value = {"response": {"status": "completed", "output": OUTPUT, "usage": USAGE,
                              "future_response": {"preserve": True}}, "future_event": [1, 2]}
        for newline in (b"\n", b"\r\n"):
            body = b"\xef\xbb\xbf" + named("response.completed", value, newline=newline, multiline=True)
            for chunk in (1, 13, 65536):
                with self.subTest(newline=newline, chunk=chunk):
                    records = []
                    response = Response(body, chunk=chunk)
                    observer = ResponseSummary(response)
                    checkpoint = CheckpointStream()
                    chunks = []
                    for data in UsageStream(response, lambda **v: records.append(v), rewrite=False).chunks():
                        chunks.append(data); observer.feed(data); checkpoint.feed(data)
                    self.assertEqual(b"".join(chunks), body)
                    self.assertEqual(records, [{"upstream_usage": USAGE}])
                    self.assertEqual(observer.finish("complete")["text"], "handoff")
                    checkpoint.feed(b"data:   [DONE]  \r\n\r\n")
                    self.assertEqual(checkpoint.finish(), "handoff")

    def test_named_completion_rewrites_only_usage_and_keeps_extensions(self):
        value = {"response": {"status": "completed", "output": OUTPUT, "usage": deepcopy(USAGE),
                              "future_response": "preserved"}, "future_event": {"v": 1}}
        body = named("response.completed", value, multiline=True)
        records = []
        result = b"".join(UsageStream(Response(body, chunk=1), lambda **v: records.append(v)).chunks())
        expected = {**value, "type": "response.completed"}
        expected["response"]["usage"].update(input_tokens=500000, total_tokens=500007)
        self.assertEqual(decode_sse(result), expected)
        self.assertIn(b"id: preserved\n", result)
        self.assertEqual(records[0]["upstream_usage"], USAGE)

    def test_capacity_advice_accepts_event_name_and_never_broadens_error_scope(self):
        for kind, payload in (("error", {"code": "server_is_overloaded", "future": [1]}),
                              ("response.failed", {"response": {"error": {"code": "server_is_overloaded"}}})):
            records = []
            result = b"".join(NativeRetryStream(Response(named(kind, payload, multiline=True), chunk=1),
                lambda **v: records.append(v), policy=lambda: (True, 30)).chunks())
            parsed = decode_sse(result)
            self.assertEqual(parsed["type"], "response.failed")
            self.assertEqual(parsed["response"]["error"]["headers"]["Retry-After"], "30")
            self.assertIn(b"event: response.failed\n", result)
            self.assertTrue(records[0]["native_retry_advised"])
        for kind, payload in (("response.failed", {"response": {"error": {"code": "invalid_prompt"}}}),
                              ("response.future", {"error": {"code": "server_is_overloaded"}}),
                              ("response.cancelled", {"response": {"error": {"code": "server_is_overloaded"}}}),
                              ("response.failed", {"type": "response.future", "error": {"code": "server_is_overloaded"}})):
            body = named(kind, payload)
            result = b"".join(NativeRetryStream(Response(body), lambda **_: self.fail("unexpected advice"),
                                              policy=lambda: (True, 30)).chunks())
            self.assertEqual(result, body)

    def test_explicit_type_wins_and_unknown_events_are_byte_exact(self):
        body = named("response.completed", {"type": "response.future", "response": {"usage": USAGE}, "new": True})
        self.assertEqual(decode_sse(body)["type"], "response.future")
        result = b"".join(UsageStream(Response(body), lambda **_: self.fail("invented usage")).chunks())
        self.assertEqual(result, body)
        for invalid in (b"event: response.completed\ndata: invalid json\n\n", b"data: [DONE]\n\n"):
            self.assertEqual(b"".join(UsageStream(Response(invalid), lambda **_: self.fail()).chunks()), invalid)

    def test_unknown_discriminator_or_unfinished_status_does_not_break_forwarding(self):
        for kind in (None, [], {"new": "envelope"}, 7):
            body = named("error", {"type": kind, "error": {"code": "server_is_overloaded"}})
            result = b"".join(NativeRetryStream(Response(body), lambda **_: self.fail("unexpected rewrite"),
                                              policy=lambda: (True, 30)).chunks())
            self.assertEqual(result, body)
        for status in ("in_progress", "queued", "future_status", [], {"new": "status"}):
            body = json.dumps({"status": status, "usage": USAGE}).encode()
            result = b"".join(UsageStream(Response(body, content_type="application/json"),
                                         lambda **_: self.fail("unfinished usage")).chunks())
            self.assertEqual(result, body)

    def test_observation_handles_bom_json_suffix_and_cancelled_terminal(self):
        cases = [(b"\xef\xbb\xbf" + named("response.cancelled", {"response": {"status": "cancelled"}}), {}),
                 (json.dumps({"status": "cancelled"}).encode(), {"Content-Type": "application/vnd.responses+json"})]
        for body, headers in cases:
            events, errors = [], []
            trace = SimpleNamespace(record=lambda event, **fields: events.append((event, fields)),
                                    journal=SimpleNamespace(_error=lambda error: errors.append(error)))
            observer = ResponseObservation(trace, headers)
            for byte in body:
                observer.feed(bytes([byte]))
            observer.finish()
            self.assertFalse(errors)
            self.assertIn(("terminal", {"event_type": "response.cancelled", "capacity": False, "error_code": None}), events)
            self.assertNotIn("observation_stopped", [event for event, _ in events])
        self.assertEqual(stream_event_class({"type": "response.cancelled"}), "terminal")

    def test_checkpoint_does_not_accept_failed_unknown_or_nontext_output(self):
        cases = [("response.failed", {"response": {"error": {"code": "invalid_prompt"}}}),
                 ("response.cancelled", {"response": {"status": "cancelled"}}),
                 ("response.future", {"response": {"status": "completed", "output": OUTPUT}}),
                 ("response.completed", {"response": {"status": "completed", "output": [{"type": "future_item"}]}})]
        for kind, value in cases:
            with self.subTest(kind=kind), self.assertRaises(OverflowFailure):
                stream = CheckpointStream()
                stream.feed(named(kind, value, multiline=True)); stream.finish()
