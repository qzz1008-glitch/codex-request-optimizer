"""Metadata compatibility must not broaden which failures can be retried."""
import json
import unittest

from packet_inspector.capacity_retry import CAPACITY_MESSAGE, open_response, stream_event_class
from tests.test_capacity_retry import CREATED, FAILED, SUCCESS, Opener, Response, frame


class CapacityStreamEventTests(unittest.TestCase):
    def run_response(self, *responses):
        opener, events, waits = Opener(*responses), [], []
        response = open_response(opener, None, enabled=True,
                                 wait=lambda delay, _: waits.append(delay),
                                 on_event=lambda event, **fields: events.append({"event": event, **fields}))
        with response:
            body = response.read()
        return body, opener, events, waits

    def test_incident_event_order_retries_before_forwarding_metadata_or_error(self):
        metadata = frame("response.metadata", metadata={"future_field": "x" * 189000})
        overloaded = frame("error", code="server_is_overloaded", message=CAPACITY_MESSAGE)
        failure = Response(CREATED + metadata + overloaded + FAILED)
        body, opener, events, waits = self.run_response(failure, Response(SUCCESS))
        self.assertEqual((body, len(opener.requests), len(waits)), (SUCCESS, 2, 1))
        self.assertTrue(failure.closed)
        self.assertIs(opener.requests[0], opener.requests[1])
        self.assertTrue(any(e["event"] == "control_event" and e["event_type"] == "response.metadata" for e in events))
        probes = [e for e in events if e["event"] == "probe_finished"]
        self.assertEqual((probes[0]["reason"], probes[0]["event_class"]), ("capacity_error", "terminal"))

    def test_metadata_family_and_opaque_extensions_do_not_need_an_event_allowlist(self):
        for kind in ("response.metadata", "codex.response.metadata", "response.metadata.updated",
                     "codex.response.metadata.future_revision"):
            with self.subTest(kind=kind):
                # These names inside metadata are not generated output or an error.
                metadata = frame(kind, metadata={"output": [{"type": "function_call"}], "delta": "private",
                                 "error": {"code": "server_is_overloaded"}}, new_field={"arbitrary": True}, status="completed")
                body, opener, _, _ = self.run_response(Response(metadata + FAILED, chunk=7), Response(SUCCESS))
                self.assertEqual((body, len(opener.requests)), (SUCCESS, 2))

    def test_metadata_on_a_successful_response_is_forwarded_byte_for_byte(self):
        body = b': heartbeat\r\n\r\n' + b'event: response.metadata\r\ndata: { "metadata": {"new":42} }\r\n\r\n' + SUCCESS
        forwarded, opener, _, waits = self.run_response(Response(body, chunk=3))
        self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_metadata_followed_by_empty_completion_does_not_retry(self):
        body = frame("response.metadata", metadata={}) + frame("response.completed", response={"output": []})
        forwarded, opener, _, waits = self.run_response(Response(body))
        self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_new_output_shapes_commit_even_when_the_event_name_is_unknown(self):
        shapes = ({"item": {"type": "future_item"}}, {"part": {}}, {"delta": ""}, {"output_index": 0},
                  {"response": {"output": [{"type": "future_item"}]}}, {"output": [{"type": "message"}]})
        for shape in shapes:
            with self.subTest(shape=shape):
                body = frame("response.metadata", metadata={}) + frame("response.future_revision", **shape) + FAILED
                forwarded, opener, events, waits = self.run_response(Response(body, chunk=5))
                self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))
                probe = next(e for e in events if e["event"] == "probe_finished")
                self.assertEqual(probe["event_class"], "output")

    def test_output_families_include_future_media_and_tool_calls(self):
        for kind in ("response.output_video.started", "response.future_tool_call.executing", "response.future.delta"):
            with self.subTest(kind=kind):
                body = frame(kind) + FAILED
                forwarded, opener, _, waits = self.run_response(Response(body))
                self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_output_evidence_wins_over_a_metadata_label_or_capacity_error(self):
        for payload in ({"type": "response.metadata", "item": {}},
                        {"type": "error", "code": "server_is_overloaded", "delta": "partial"}):
            with self.subTest(payload=payload):
                body = b'data: ' + json.dumps(payload).encode() + b'\n\n' + FAILED
                forwarded, opener, _, waits = self.run_response(Response(body))
                self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_empty_reasoning_tool_calls_and_ciphertext_still_prevent_replay(self):
        items = ({"type": "reasoning", "summary": []},
                 {"type": "function_call", "arguments": ""},
                 {"type": "future_item", "encrypted_content": "private"})
        for item in items:
            with self.subTest(item=item):
                body = frame("response.metadata", metadata={}) + frame("response.output_item.added", item=item) + FAILED
                forwarded, opener, _, waits = self.run_response(Response(body))
                self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_unknown_semantics_pass_through_without_enabling_retry(self):
        body = frame("response.future_revision", future_field={}) + FAILED
        forwarded, opener, events, waits = self.run_response(Response(body))
        self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))
        probe = next(e for e in events if e["event"] == "probe_finished")
        self.assertEqual((probe["reason"], probe["event_class"]), ("unknown_event", "unknown"))

    def test_non_capacity_errors_are_never_retried_after_metadata(self):
        for status in (200, 429, 500, 502, 503, 504):
            for code in ("rate_limit_exceeded", "maintenance", "invalid_prompt", "server_error"):
                with self.subTest(status=status, code=code):
                    body = frame("response.metadata", metadata={}) + frame("response.failed", response={"error": {"code": code}})
                    forwarded, opener, _, waits = self.run_response(Response(body, status=status))
                    self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_401_403_413_never_retry_even_with_a_capacity_error_body(self):
        for status in (401, 403, 413):
            for kind in ("application/json", "text/event-stream", "text/html"):
                with self.subTest(status=status, kind=kind):
                    body = json.dumps({"error": {"code": "server_is_overloaded"}}).encode()
                    forwarded, opener, _, waits = self.run_response(Response(body, status=status, content_type=kind))
                    self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_other_json_errors_and_quoted_capacity_text_do_not_retry(self):
        for payload in ({"error": {"code": "server_error"}},
                        {"metadata": {"error": {"code": "server_is_overloaded"}}},
                        {"error": {"code": "other", "message": "Quoted: " + CAPACITY_MESSAGE}},
                        {"input": CAPACITY_MESSAGE}):
            with self.subTest(payload=payload):
                body = json.dumps(payload).encode()
                forwarded, opener, _, waits = self.run_response(Response(body, status=503, content_type="application/json"))
                self.assertEqual((forwarded, len(opener.requests), waits), (body, 1, []))

    def test_connection_failures_do_not_create_capacity_retries(self):
        for error in (TimeoutError("test timeout"), ConnectionResetError("test reset"), OSError("test network")):
            with self.subTest(error=type(error).__name__):
                opener = Opener(error)
                with self.assertRaises(type(error)):
                    open_response(opener, None, enabled=True, wait=lambda *_: self.fail("Unexpected retry"))
                self.assertEqual(len(opener.requests), 1)

    def test_classifier_handles_malformed_types_without_guessing(self):
        for payload in (None, [], {"type": {}}, {"type": []}, {"type": 1}, {"type": "future", "response": []}):
            with self.subTest(payload=payload):
                self.assertEqual(stream_event_class(payload), "unknown")

    def test_json_terminal_status_is_not_misreported_as_an_unknown_event(self):
        for status in ("completed", "failed", "incomplete"):
            with self.subTest(status=status):
                self.assertEqual(stream_event_class({"status": status, "output": []}), "terminal")


if __name__ == "__main__":
    unittest.main()
