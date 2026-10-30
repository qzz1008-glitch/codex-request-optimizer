"""Sharing accounts for upstream numbers without retaining private content."""
import io
import json
import unittest
from unittest.mock import patch

from packet_inspector.sharing_usage import SharedUsageStream


class Response(io.BytesIO):
    def __init__(self, body, content_type="text/event-stream", status=200, encoding="identity"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type, "Content-Encoding": encoding}


def terminal(kind="response.completed", **usage):
    value = {"type": kind, "response": {"id": "private-response-id", "output": [{"text": "private reply"}],
             "usage": {"input_tokens": 37, "output_tokens": 2, "total_tokens": 39, **usage}}}
    return b"data: " + json.dumps(value).encode() + b"\n\n"


class SharingUsageTests(unittest.TestCase):
    def test_chunks_are_unchanged_and_first_output_does_not_wait_for_completion(self):
        first = b'data: {"type":"response.output_text.delta","delta":"private reply"}\n\n'
        parts = iter((first, terminal(), b""))
        response = Response(b"")
        response.read1 = lambda _size: next(parts)
        records = []
        observer = SharedUsageStream(response, records.append)
        chunks = observer.chunks()
        self.assertEqual(next(chunks), first)
        self.assertEqual(records, [])
        self.assertEqual(b"".join(chunks), terminal())
        self.assertEqual(records, [{"input_tokens": 37, "output_tokens": 2, "total_tokens": 39}])

    def test_compact_json_and_cache_are_numeric_only(self):
        raw = json.dumps({"object": "response.compaction", "output": [{"encrypted_content": "private"}],
            "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100,
                      "input_tokens_details": {"cached_tokens": 60}, "attribution": "private"}}).encode()
        records = []
        observer = SharedUsageStream(Response(raw, "application/json"), records.append)
        self.assertEqual(b"".join(observer.chunks()), raw)
        self.assertEqual(records, [{"input_tokens": 90, "output_tokens": 10, "total_tokens": 100,
                                    "cached_input_tokens": 60}])

    def test_duplicate_terminal_usage_is_counted_once_and_failed_usage_is_real(self):
        for kind in ("response.completed", "response.failed", "response.incomplete", "response.cancelled"):
            with self.subTest(kind=kind):
                raw = terminal(kind) * 2
                records = []
                observer = SharedUsageStream(Response(raw), records.append)
                self.assertEqual(b"".join(observer.chunks()), raw)
                self.assertEqual(len(records), 1)

    def test_split_frames_and_missing_final_newline_are_supported(self):
        raw = terminal().rstrip(b"\n")
        response = Response(b"")
        chunks = iter([raw[i:i+1] for i in range(len(raw))] + [b""])
        response.read1 = lambda _size: next(chunks)
        records = []
        observer = SharedUsageStream(response, records.append)
        self.assertEqual(b"".join(observer.chunks()), raw)
        self.assertTrue(observer.metered)

    def test_invalid_or_unavailable_usage_is_not_estimated(self):
        for raw in (b'data: {"type":"response.created"}\n\n', b'bad json', terminal(input_tokens=True),
                    terminal(input_tokens=-1), terminal(total_tokens=3.5)):
            with self.subTest(raw=raw):
                records = []
                observer = SharedUsageStream(Response(raw), records.append)
                self.assertEqual(b"".join(observer.chunks()), raw)
                self.assertFalse(observer.metered)
                self.assertEqual(records, [])

    def test_observation_limits_and_failures_never_break_forwarding(self):
        raw = b'data: {"delta":"' + b'x' * 1024 + b'"}\n\n' + terminal()
        records = []
        with patch("packet_inspector.usage_trigger.MAX_EVENT_BYTES", 512):
            observer = SharedUsageStream(Response(raw), records.append)
            self.assertEqual(b"".join(observer.chunks()), raw)
        self.assertEqual(len(records), 1)
        def fail(_usage):
            raise OSError("unavailable accounting")
        observer = SharedUsageStream(Response(terminal()), fail)
        self.assertEqual(b"".join(observer.chunks()), terminal())
        self.assertFalse(observer.metered)

    def test_unexpected_response_encoding_is_passed_through_without_fake_tokens(self):
        records = []
        observer = SharedUsageStream(Response(b"encoded bytes", encoding="gzip"), records.append)
        self.assertEqual(b"".join(observer.chunks()), b"encoded bytes")
        self.assertFalse(observer.metered)
