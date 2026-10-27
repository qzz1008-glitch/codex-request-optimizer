"""Synthetic summaries, bounded storage and backwards-compatible usage cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from packet_inspector.analyzer import encode
from packet_inspector.response_summary import ResponseSummary, MAX_EVENT_BYTES, MAX_SUMMARY_BYTES
from packet_inspector.storage import Store
from packet_inspector.usage_trigger import UsageStream
from tests.response_fixtures import Response, frame


USAGE = {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130,
         "input_tokens_details": {"cached_tokens": 100}, "future_usage": 17,
         "attribution": {"items": {"opaque": {"input_tokens": 123, "output_tokens": 7}}}}


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def collect(payload, mode="text/event-stream", chunk=7, state="complete", **kwargs):
    response = Response(payload, content_type=mode, chunk=chunk, **kwargs)
    observer = ResponseSummary(response)
    while data := response.read1():
        observer.feed(data)
    return observer.finish(state)


class ResponseSummaryTests(unittest.TestCase):
    def test_split_stream_uses_final_visible_output_without_duplicate_deltas_or_reasoning(self):
        output = [message("你好，已完成。"), {"type": "reasoning", "summary": [{"text": "private reasoning"}]},
                  {"type": "function_call", "name": "functions.exec", "arguments": "private args"},
                  {"type": "image_generation_call", "result": "private media"}]
        payload = b"\xef\xbb\xbf" + frame("response.created", response={"id": "resp-test", "status": "in_progress"})
        payload += frame("response.output_text.delta", delta="你好，")
        payload += frame("response.reasoning_summary_text.delta", delta="private reasoning")
        payload += frame("response.completed", response={"id": "resp-test", "status": "completed", "output": output})
        for chunk in (1, 7, 65536):
            summary = collect(payload, chunk=chunk)
            self.assertEqual(summary["text"], "你好，已完成。")
            self.assertEqual(summary["tools"], ["functions.exec", "image_generation_call"])
            self.assertEqual((summary["status"], summary["response_id"]), ("completed", "resp-test"))
            self.assertNotIn("private", json.dumps(summary))

    def test_json_media_types_missing_type_and_final_frame_without_separator(self):
        value = {"id": "resp-json", "status": "completed", "output": [message("done")], "usage": USAGE}
        for mode in ("application/json", "application/response+json", None):
            self.assertEqual(collect(encode(value), mode)["text"], "done")
        payload = frame("response.completed", response=value).rstrip(b"\n")
        self.assertEqual(collect(payload)["status"], "completed")

    def test_truncated_partial_stream_is_bounded_and_redacts_cross_delta_credentials(self):
        secret = "sk-" + "a" * 40
        payload = frame("response.output_text.delta", delta=secret[:15])
        payload += frame("response.output_text.delta", delta=secret[15:] + "界" * 3000)
        for i in range(30):
            payload += frame("response.output_item.added", item={"type": "function_call", "name": "tool" + str(i) + "界" * 80})
        summary = collect(payload, state="client_disconnected")
        self.assertEqual(summary["status"], "interrupted")
        self.assertTrue(summary["text_truncated"])
        self.assertTrue(summary["tools_truncated"])
        self.assertLessEqual(len(summary["tools"]), 8)
        self.assertLessEqual(len(encode(summary)), MAX_SUMMARY_BYTES)
        self.assertFalse(secret in json.dumps(summary))
        self.assertTrue("[credential redacted]" in summary["text"])

    def test_oversized_invalid_and_encoded_responses_never_claim_complete_output(self):
        payload = b"data: " + b"x" * (MAX_EVENT_BYTES + 10) + b"\n\n"
        payload += frame("response.failed", response={"status": "failed"})
        summary = collect(payload, chunk=65536)
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["observation_limited"])
        encoded = collect(b"opaque encoded data", extra={"Content-Encoding": "gzip"})
        self.assertEqual(encoded["status"], "unknown")
        self.assertTrue(encoded["observation_limited"])
        malformed = collect(b"data: invalid\n\n")
        self.assertTrue(malformed["observation_limited"])

    def test_refusals_tool_only_and_incomplete_output_have_authentic_states(self):
        for status in ("incomplete", "failed", "cancelled"):
            response = {"status": status, "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "Unable to proceed"}]}]}
            self.assertEqual(collect(frame("response." + status, response=response))["status"], status)
        tool = collect(frame("response.completed", response={"output": [{"type": "custom_tool_call", "name": "apply_patch", "input": "private"}]}))
        self.assertEqual(tool["text"], "")
        self.assertEqual(tool["tools"], ["apply_patch"])

    def test_hidden_output_and_whitespace_without_content_type_are_bounded(self):
        payload = frame("response.output_item.added", output_index=0, item={"type": "message", "channel": "analysis"})
        payload += frame("response.output_text.delta", output_index=0, delta="private analysis")
        payload += frame("response.completed", response={"output": [{"type": "reasoning"}]})
        self.assertEqual(collect(payload)["text"], "")
        summary = collect(b" " * (MAX_EVENT_BYTES + 10), mode=None, chunk=65536)
        self.assertTrue(summary["observation_limited"])
        self.assertEqual(summary["text"], "")

    def test_usage_compaction_never_changes_forwarded_attribution_or_authentic_counts(self):
        value = {"status": "completed", "usage": deepcopy(USAGE)}
        payload = frame("response.completed", response=value)
        for rewrite in (False, True):
            records = []
            forwarded = b"".join(UsageStream(Response(payload), lambda **fields: records.append(fields), rewrite=rewrite).chunks())
            if not rewrite:
                self.assertEqual(forwarded, payload)
            response = json.loads(forwarded[6:])["response"]
            self.assertEqual(response["usage"]["attribution"], USAGE["attribution"])
            for key in ("upstream_usage", "reported_usage"):
                if key in records[0]:
                    self.assertNotIn("attribution", records[0][key])
                    self.assertEqual(records[0][key]["future_usage"], 17)
            self.assertEqual(records[0]["upstream_usage"]["input_tokens"], 123)


class SummaryRetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_summary_survives_detail_eviction_and_is_deleted_with_its_request(self):
        store = Store(self.root, max_count=2, max_detail_count=0)
        summary = collect(frame("response.completed", response={"id": "resp-retain", "output": [message("saved excerpt")]}))
        first = store.add({"method": "POST", "thread_id": "a", "upstream_usage": USAGE}, {"body": {"input": "private"}})
        store.update(first, state="complete", status=200, response_summary=summary, reported_usage=USAGE)
        meta, content = store.get(first, content=True)
        self.assertIsNone(content)
        self.assertTrue(meta["details_evicted"])
        self.assertEqual(meta["response_summary"], summary)
        self.assertNotIn("attribution", meta["upstream_usage"])
        self.assertEqual(store.stats()["count"], 1)
        store.add({"method": "POST", "thread_id": "b"})
        store.add({"method": "POST", "thread_id": "c"})
        with self.assertRaises(KeyError):
            store.get(first)
        self.assertEqual(store.stats()["count"], 2)

    def test_bounded_legacy_cleanup_preserves_body_summary_and_all_counters(self):
        store = Store(self.root)
        packets = []
        for i in range(2):
            packet = store.add({"method": "POST", "thread_id": "legacy", "response_summary": {"text": "existing excerpt"}}, {"body": {"input": "private"}})
            meta, _ = store.get(packet)
            meta.update(state="complete", upstream_usage=deepcopy(USAGE), reported_usage=deepcopy(USAGE))
            meta["upstream_usage"]["attribution"]["items"]["opaque"]["padding"] = "x" * 50000
            with store.connect() as db:
                db.execute("UPDATE packets SET meta=? WHERE id=?", (json.dumps(meta), packet))
                original = db.execute("SELECT content FROM packets WHERE id=?", (packet,)).fetchone()[0]
            packets.append((packet, original))
        before = store.stats()["stored"]
        self.assertEqual(store.compact_usage_records(limit=1), 1)
        self.assertLess(store.stats()["stored"], before - 40000)
        # Startup handles remaining legacy data; subsequent sweeps are idempotent.
        store = Store(self.root)
        self.assertEqual(store.compact_usage_records(), 0)
        for packet, original in packets:
            meta, _ = store.get(packet)
            self.assertEqual(meta["upstream_usage"], {k: v for k, v in USAGE.items() if k != "attribution"})
            self.assertEqual(meta["reported_usage"], meta["upstream_usage"])
            self.assertEqual(meta["response_summary"]["text"], "existing excerpt")
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT content FROM packets WHERE id=?", (packet,)).fetchone()[0], original)
