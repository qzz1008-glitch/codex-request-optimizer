"""Shared framing keeps raw bytes and the consumers' distinct EOF policies."""
from types import SimpleNamespace
import unittest

from packet_inspector.response_events import SSEBuffer
from packet_inspector.response_summary import ResponseSummary
from packet_inspector.retry_diagnostics import ResponseObservation
from packet_inspector.usage_trigger import UsageStream
from tests.response_fixtures import Response, frame


class StreamBoundaryTests(unittest.TestCase):
    def test_every_two_piece_split_preserves_bom_delimiters_and_trailing_bytes(self):
        payload = b"\xef\xbb\xbf: keepalive\n\ndata: first\r\n\r\ndata: second\n\r\ntail"
        for split in range(1, len(payload)):
            with self.subTest(split=split):
                parser = SSEBuffer(256)
                parsed = []
                for chunk in (payload[:split], payload[split:]):
                    parser.buffer.extend(chunk)
                    parsed.extend(parser.frames())
                self.assertEqual([separator for _, separator, _ in parsed], [b"\n\n", b"\r\n\r\n", b"\n\r\n"])
                self.assertTrue(all(not skipped for _, _, skipped in parsed))
                self.assertEqual(b"".join(raw + separator for raw, separator, _ in parsed) + bytes(parser.buffer), payload)

    def test_dropped_frame_recovers_across_a_split_crlf_delimiter(self):
        parser = SSEBuffer(16)
        parser.buffer.extend(b"data: " + b"x" * 40 + b"\r\n\r")
        self.assertTrue(parser.discard_excess())
        self.assertLessEqual(len(parser.buffer), 3)
        parser.buffer.extend(b"\ndata: ok\n\n")
        parsed = list(parser.frames())
        self.assertEqual(parsed, [(b"", b"\r\n\r\n", True), (b"data: ok", b"\n\n", False)])

    def test_forwarding_an_oversized_frame_preserves_every_byte(self):
        parser = SSEBuffer(16)
        prefix = b"data: " + b"x" * 40 + b"\r\n\r"
        parser.buffer.extend(prefix)
        forwarded = parser.release_excess()
        parser.buffer.extend(b"\ndata: ok\n\n")
        forwarded += b"".join(raw + separator for raw, separator, _ in parser.frames())
        self.assertEqual(forwarded, prefix + b"\ndata: ok\n\n")

    def test_frame_at_the_limit_is_accepted_and_a_larger_frame_is_skipped(self):
        parser = SSEBuffer(16)
        parser.buffer.extend(b"a" * 16 + b"\n\n" + b"b" * 17 + b"\n\n")
        self.assertEqual([skipped for _, _, skipped in parser.frames()], [False, True])

    def test_consumers_keep_their_distinct_final_frame_rules(self):
        usage = {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130}
        payload = frame("response.completed", response={"status": "completed", "usage": usage})[:-2]
        response = Response(payload, chunk=1)
        summary = ResponseSummary(response)
        records, events = [], []
        trace = SimpleNamespace(journal=SimpleNamespace(_error=lambda exc: self.fail(str(exc))),
                                record=lambda event, **fields: events.append((event, fields)))
        observer = ResponseObservation(trace, response.headers)
        forwarded = []
        for chunk in UsageStream(response, lambda **fields: records.append(fields), rewrite=False).chunks():
            forwarded.append(chunk)
            summary.feed(chunk)
            observer.feed(chunk)
        observer.finish()
        self.assertEqual(b"".join(forwarded), payload)
        self.assertTrue(all(len(chunk) == 1 for chunk in forwarded))
        self.assertEqual(records, [{"upstream_usage": usage}])
        self.assertEqual(summary.finish("complete")["status"], "completed")
        self.assertIn(("observation_stopped", {"reason": "incomplete_event"}), events)
        self.assertFalse(any(event == "terminal" for event, _ in events))
