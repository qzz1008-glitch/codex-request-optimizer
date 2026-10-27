"""Retain chart metadata independently of bounded request details."""
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.analyzer import encode, inspect_request
from packet_inspector.storage import Store


class TieredRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = 1000

    def add(self, store, index, state="complete"):
        text = "private-body-" + str(index) + ":" + random.Random(index).randbytes(2048).hex()
        raw = encode({"model": "test", "input": [{"role": "user", "content": text}]})
        meta, content = inspect_request(raw, {"session_id": "retained-session", "Content-Type": "application/json"}, "/responses")
        meta.update(method="POST", path="/responses", request_purpose="generation",
                    parent_thread_id="parent", usage_override_applied=True, relay_zstd_level=3)
        self.clock += 1
        with patch("packet_inspector.storage.time.time", return_value=self.clock):
            packet = store.add(meta, content)
        store.update(packet, state=state, status=200, outbound_wire_bytes=1234,
                     upstream_usage={"input_tokens": index + 100, "output_tokens": 7, "total_tokens": index + 107})
        return packet, content

    def test_chart_range_tracks_retained_requests_without_loading_details(self):
        store = Store(self.root, max_count=5, max_detail_count=0)
        self.assertIsNone(store.stats()["oldest_created"])
        with patch("packet_inspector.storage.time.time", return_value=900):
            store.add({"method": "GET", "path": "/models"})
        self.assertIsNone(store.stats()["oldest_created"])
        for index in range(3):
            self.add(store, index)
        with patch("packet_inspector.storage.json.loads", side_effect=AssertionError("loaded request JSON")):
            self.assertEqual(store.stats()["oldest_created"], 1001)
        store.configure_limits(1_000_000, 2, 0)
        self.assertEqual(store.stats()["oldest_created"], 1002)
        store.configure_limits(1, 2, 0)
        self.assertIsNone(store.stats()["oldest_created"])

    def test_basic_records_keep_all_frontend_statistics_without_body_or_snippets(self):
        store = Store(self.root, max_count=5, max_detail_count=2)
        packets = [self.add(store, index) for index in range(5)]
        meta, body = store.get(packets[0][0], content=True)
        self.assertIsNone(body)
        self.assertTrue(meta["details_evicted"])
        self.assertNotIn("input_items", meta)
        self.assertNotIn("headers", meta)
        self.assertNotIn("private-body-", json.dumps(meta))
        for field in ("id", "created", "time", "thread_id", "parent_thread_id", "model", "method", "path",
                      "state", "status", "wire_bytes", "decoded_bytes", "outbound_wire_bytes", "top_fields",
                      "input_groups", "json_envelope_bytes", "pairing", "upstream_usage", "usage_override_applied",
                      "relay_zstd_level", "classification_version"):
            self.assertIn(field, meta, field)
        self.assertEqual(meta["upstream_usage"]["input_tokens"], 100)
        for packet, expected in packets[-2:]:
            recent, actual = store.get(packet, content=True)
            self.assertEqual(actual, expected)
            self.assertIn("input_items", recent)
            self.assertNotIn("details_evicted", recent)
        rows = store.list(thread="retained-session", sort="created", direction="asc")
        self.assertEqual([row["id"] for row in rows], [packet for packet, _ in packets])
        self.assertEqual([row["upstream_usage"]["input_tokens"] for row in rows], list(range(100, 105)))
        self.assertEqual((store.stats()["count"], store.stats()["detail_count"]), (5, 2))

    def test_record_limit_is_global_and_ignores_request_state(self):
        store = Store(self.root, max_count=4, max_detail_count=1)
        packets = [self.add(store, index, state) for index, state in enumerate(
            ("streaming", "captured", "relay_error", "complete", "captured", "streaming"))]
        self.assertEqual(store.stats()["count"], 4)
        self.assertEqual(store.stats()["detail_count"], 1)
        self.assertEqual([row["id"] for row in store.list()], [packet for packet, _ in reversed(packets[-4:])])
        for packet, _ in packets[:2]:
            with self.assertRaises(KeyError):
                store.get(packet)

    def test_storage_pressure_discards_old_details_before_basic_records(self):
        store = Store(self.root, max_count=4, max_detail_count=4)
        packets = [self.add(store, index) for index in range(4)]
        budget = store.stats()["stored"] - 1
        store.configure_limits(budget, 4, 4)
        self.assertEqual(store.stats()["count"], 4)
        self.assertEqual(store.stats()["detail_count"], 3)
        self.assertLessEqual(store.stats()["stored"], budget)
        self.assertIsNone(store.get(packets[0][0], content=True)[1])
        for packet, content in packets[1:]:
            self.assertEqual(store.get(packet, content=True)[1], content)

    def test_storage_budget_keeps_target_record_count_as_new_requests_arrive(self):
        store = Store(self.root, max_count=5)
        packets = [self.add(store, index) for index in range(5)]
        budget = store.stats()["stored"] - 1
        store.configure_limits(budget, 5)
        for index in range(5, 10):
            packets.append(self.add(store, index))
            stats = store.stats()
            self.assertEqual(stats["count"], 5)
            self.assertLessEqual(stats["stored"], budget)
            self.assertGreater(stats["detail_count"], 0)
            self.assertLess(stats["detail_count"], stats["count"])
            self.assertEqual([row["id"] for row in store.list()],
                             [packet for packet, _ in reversed(packets[-5:])])

    def test_more_space_keeps_more_new_details_without_a_separate_count_limit(self):
        store = Store(self.root, max_count=20)
        packets = [self.add(store, index) for index in range(5)]
        store.configure_limits(store.stats()["stored"] - 1, 20)
        self.assertEqual(store.stats()["detail_count"], 4)
        store.configure_limits(2_000_000, 20)
        for index in range(5, 10):
            self.add(store, index)
        stats = store.stats()
        self.assertEqual((stats["count"], stats["detail_count"]), (10, 9))
        self.assertIsNone(store.get(packets[0][0], content=True)[1])

    def test_basic_records_are_also_bounded_when_no_details_fit(self):
        store = Store(self.root, max_count=8, max_detail_count=8)
        packets = [self.add(store, index, "streaming") for index in range(8)]
        store.configure_limits(2200, 8, 8)
        stats = store.stats()
        self.assertGreater(stats["count"], 0)
        self.assertLess(stats["count"], 8)
        self.assertEqual(stats["detail_count"], 0)
        self.assertLessEqual(stats["stored"], 2200)
        self.assertEqual(store.list()[0]["id"], packets[-1][0])
        self.assertTrue(all(row.get("details_evicted") for row in store.list()))

    def test_zero_details_skips_body_storage_but_keeps_usage_and_statistics(self):
        store = Store(self.root, max_count=5, max_detail_count=0)
        with patch("packet_inspector.storage.zlib.compress") as compress:
            packet, _ = self.add(store, 1, "streaming")
        compress.assert_not_called()
        meta, body = store.get(packet, content=True)
        self.assertIsNone(body)
        self.assertEqual(meta["upstream_usage"]["input_tokens"], 101)
        self.assertIn("top_fields", meta)
        self.assertNotIn("input_items", meta)
        self.assertEqual(store.stats()["detail_count"], 0)

    def test_limit_changes_apply_immediately_and_never_restore_removed_details(self):
        store = Store(self.root, max_count=5, max_detail_count=5)
        packets = [self.add(store, index) for index in range(4)]
        store.configure_limits(1024 * 1024, 5, 1)
        self.assertEqual(store.stats()["detail_count"], 1)
        store.configure_limits(1024 * 1024, 5, 5)
        self.assertEqual(store.stats()["detail_count"], 1)
        old = packets[0][0]
        store.update(old, state="complete", headers={"secret": "old header"},
                     input_items=[{"label": "private-body-0"}], details_evicted=False,
                     upstream_usage={"input_tokens": 999})
        meta, body = store.get(old, content=True)
        self.assertIsNone(body)
        self.assertTrue(meta["details_evicted"])
        self.assertNotIn("headers", meta)
        self.assertNotIn("input_items", meta)
        self.assertEqual(meta["upstream_usage"]["input_tokens"], 999)
        self.add(store, 5)
        self.assertEqual(store.stats()["detail_count"], 2)

    def test_only_latest_m_requests_are_eligible_even_after_details_are_missing(self):
        store = Store(self.root, max_count=10, max_detail_count=3)
        first, _ = self.add(store, 1)
        store.configure_limits(1024 * 1024, 10, 0)
        store.configure_limits(1024 * 1024, 10, 2)
        second, _ = self.add(store, 2)
        self.clock += 1
        with patch("packet_inspector.storage.time.time", return_value=self.clock):
            empty = store.add({"method": "GET", "path": "/models"})
        self.assertIsNone(store.get(first, content=True)[1])
        self.assertFalse(store.get(second)[0].get("details_evicted", False))
        self.assertFalse(store.get(empty)[0].get("details_evicted", False))
        self.add(store, 4)
        self.assertTrue(store.get(second)[0]["details_evicted"])
        self.assertEqual(store.stats()["detail_count"], 2)

    def test_restart_migrates_existing_records_without_changing_chart_metadata(self):
        store = Store(self.root, max_count=5)
        packets = [self.add(store, index) for index in range(4)]
        before = store.get(packets[0][0])[0]
        reopened = Store(self.root, max_count=5, max_detail_count=1)
        after, body = reopened.get(packets[0][0], content=True)
        self.assertIsNone(body)
        for key in ("created", "thread_id", "top_fields", "input_groups", "upstream_usage", "outbound_wire_bytes"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(reopened.stats()["count"], 4)
        self.assertEqual(reopened.stats()["detail_count"], 1)
        with reopened.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            total = db.execute("SELECT sum(coalesce(length(content),0)+length(cast(meta AS BLOB))) FROM packets").fetchone()[0]
            self.assertEqual(total, reopened.stats()["stored"])
            self.assertEqual(len(db.execute("PRAGMA table_info(packets)").fetchall()), 7)

    def test_basic_records_compare_statistics_without_inventing_item_changes(self):
        store = Store(self.root, max_count=5, max_detail_count=1)
        first, _ = self.add(store, 1)
        second, _ = self.add(store, 22)
        self.assertIsNone(store.compare(first))
        stored = store.stats()["stored"]
        diff = store.compare(second)
        self.assertEqual(diff["state"], "summary_only")
        self.assertEqual(diff["previous_id"], first)
        self.assertEqual(diff["decoded_delta"], 1)
        before = next(field["bytes"] for field in store.get(first)[0]["top_fields"] if field["name"] == "input")
        self.assertEqual(diff["fields"], [{"name": "input", "before": before, "after": before + 1}])
        self.assertEqual(store.stats()["stored"], stored)
        self.assertNotIn("added", diff)
        self.assertNotIn("removed", diff)
        store.configure_limits(1_000_000, 5, 0)
        reopened = Store(self.root, max_count=5, max_detail_count=0)
        self.assertEqual(reopened.compare(second), diff)


if __name__ == "__main__":
    unittest.main()
