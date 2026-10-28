"""Native estimator fixtures and authenticated-prefix display estimates."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.token_estimate import estimate_request_tokens, request_basis, calibrate
from packet_inspector.storage import Store
from packet_inspector.token_window import TokenWindows
from packet_inspector.session_summary import SessionSummaries
from packet_inspector.request_context import trend_packet_metadata


class NativeEstimateTests(unittest.TestCase):
    def estimate(self, item):
        return estimate_request_tokens({"input": [item]})["input_tokens"]

    def test_native_content_fixtures_exclude_transport_envelopes(self):
        fixtures = [
            ({"role": "user", "content": "汉字🙂", "id": "x" * 10000}, 3),
            ({"type": "reasoning", "encrypted_content": "x" * 1000}, 25),
            ({"type": "reasoning", "summary": [{"text": "not replayed"}]}, 0),
            ({"type": "compaction", "encrypted_content": "x" * 1000}, 84),
            ({"type": "custom_tool_call", "name": "x", "input": "{}"}, 3),
            ({"type": "function_call_output", "call_id": "a", "name": "foo", "namespace": "x", "output": "1234"}, 3),
            ({"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "not opened"}]}, 1844),
            ({"type": "message", "role": "user", "content": [{"type": "input_image", "file_id": "not opened", "detail": "original"}]}, 10000),
            ({"type": "custom_tool_call_output", "call_id": "", "output": [{"type": "encrypted_content", "encrypted_content": "x" * 16}]}, 3),
            ({"type": "configuration_update", "metadata": "x" * 10000}, 0),
        ]
        for item, expected in fixtures:
            with self.subTest(kind=item.get("type")):
                self.assertEqual(self.estimate(item), expected)

    def test_base_instructions_are_not_counted_twice(self):
        body = {"instructions": "a" * 40, "input": [{"type": "additional_tools", "tools": []}]}
        self.assertEqual(estimate_request_tokens(body)["input_tokens"], 1)
        self.assertEqual(estimate_request_tokens({"instructions": "a" * 40, "input": []})["input_tokens"], 10)

    def test_unavailable_media_and_server_state_are_not_guessed(self):
        for body in [
            {"input": [{"role": "user", "content": [{"type": "input_image", "detail": "original", "image_url": "not opened"}]}]},
            {"input": [{"role": "user", "content": [{"type": "input_audio", "audio_url": "not opened"}]}]},
            {"input": [], "previous_response_id": "server-owned"},
        ]:
            self.assertEqual(estimate_request_tokens(body)["state"], "unavailable")

    def test_calibration_requires_identical_prefix_and_configuration(self):
        old = {"model": "m", "input": [{"role": "user", "content": "aaaa"}]}
        new = {**old, "input": [*old["input"], {"type": "custom_tool_call_output", "call_id": "", "output": "bbbb"}]}
        result = calibrate(request_basis(new), new, request_basis(old), 100, "authentic")
        self.assertEqual(result["input_tokens"], 101)
        self.assertEqual(result["basis_packet_id"], "authentic")
        for edited in [{**new, "model": "other"}, {**new, "input": [{"role": "user", "content": "edit"}]}]:
            self.assertNotIn("basis_packet_id", calibrate(request_basis(edited), edited, request_basis(old), 100, "authentic"))


class StoredEstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "cache")
        self.old = {"model": "m", "input": [{"role": "user", "content": "aaaa"}]}
        self.new = {**self.old, "input": [*self.old["input"], {"type": "custom_tool_call_output", "call_id": "", "output": "bbbb"}]}
        self.meta = {"thread_id": "00000000-0000-4000-8000-000000000001", "model": "m", "method": "POST", "path": "/responses"}

    def add(self, body, *, overflow=False, actual=100, reported=500000):
        identifier = self.store.add(self.meta, {"body": body})
        self.store.update(identifier, state="complete", status=200,
            overflow_compaction_calls=int(overflow),
            upstream_usage={"input_tokens": actual, "output_tokens": 5, "total_tokens": actual + 5},
            reported_usage={"input_tokens": reported, "output_tokens": 5, "total_tokens": reported + 5},
            usage_override_applied=True)
        return identifier

    def test_summary_only_anchor_survives_detail_eviction_and_never_uses_reported_usage(self):
        old = self.add(self.old)
        with self.store.connect() as db:
            db.execute("UPDATE packets SET content=NULL WHERE id=?", (old,))
        current = self.add(self.new, overflow=True, actual=15)
        meta, _ = self.store.get(current)
        self.assertEqual(meta["request_token_estimate"]["input_tokens"], 101)
        reader = TokenWindows(self.root / "missing-native-home")
        with patch.object(reader, "lookup", side_effect=AssertionError("Must not read synthetic native events")):
            usage = reader.lookup_ready(meta)
        self.assertEqual(usage["input_tokens"], 101)
        self.assertTrue(usage["estimated"])
        self.assertEqual(usage["upstream_usage"]["input_tokens"], 15)
        session = SessionSummaries(reader).read(self.root / "cache")["sessions"][0]
        self.assertEqual(session["token_window"]["input_tokens"], 101)
        self.assertTrue(session["token_window"]["estimated"])
        projected = trend_packet_metadata(meta)
        self.assertEqual(projected["request_token_estimate"]["input_tokens"], 101)
        self.assertNotIn("history_sha256", projected["request_token_estimate"])

    def test_legacy_request_is_estimated_lazily_from_retained_copy(self):
        old = self.add(self.old)
        current = self.add(self.new, overflow=True, actual=15)
        with self.store.connect() as db:
            for identifier in [old, current]:
                meta = json.loads(db.execute("SELECT meta FROM packets WHERE id=?", (identifier,)).fetchone()[0])
                meta.pop("request_token_estimate")
                db.execute("UPDATE packets SET meta=? WHERE id=?", (json.dumps(meta), identifier))
        meta, body = self.store.get(current)
        self.assertIsNone(body)
        self.assertEqual(meta["request_token_estimate"]["input_tokens"], 101)
        self.assertTrue(meta["request_token_estimate"]["retained_copy"])
        self.assertEqual(self.store.get(current, content=True)[1]["body"], self.new)

    def test_missing_legacy_body_does_not_relabel_actual_usage_as_pre_summary(self):
        current = self.add(self.new, overflow=True, actual=15)
        with self.store.connect() as db:
            meta = json.loads(db.execute("SELECT meta FROM packets WHERE id=?", (current,)).fetchone()[0])
            meta.pop("request_token_estimate")
            db.execute("UPDATE packets SET meta=?,content=NULL WHERE id=?", (json.dumps(meta), current))
        meta, _ = self.store.get(current)
        self.assertEqual(TokenWindows(self.root).lookup_ready(meta)["state"], "unavailable")
        self.assertEqual(meta["upstream_usage"]["input_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
