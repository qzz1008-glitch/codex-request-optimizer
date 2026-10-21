"""Token displays must use the selected request's local usage, never byte estimates."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.token_window import TokenWindows

THREAD = "01a1592b-c6ab-7ab1-8c9b-8e74da992bcb"


def event(at=102, inputs=40000, outputs=500, window=270000, cumulative=999999):
    return {"type": "event_msg", "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": inputs, "cached_input_tokens": 39000,
                                     "output_tokens": outputs, "total_tokens": inputs + outputs},
                "total_token_usage": {"total_tokens": cumulative}, "model_context_window": window}}}


class TokenWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / "sessions" / "rollout-test.jsonl"
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", (THREAD, str(self.path)))
            db.commit()
        self.reader = TokenWindows(self.home)
        self.packet = {"method": "POST", "thread_id": THREAD, "model": "gpt-6.1-sol", "created": 100,
                       "duration_ms": 1000, "status": 200, "state": "complete"}
        self.prefix = [{"type": "session_meta", "payload": {"id": THREAD}},
                       {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *events):
        self.path.write_text("".join(json.dumps(e) + "\n" for e in [*self.prefix, *events]), encoding="utf8")

    def test_uses_single_response_usage_and_historical_window(self):
        self.write(event())
        result = self.reader.lookup(self.packet, 103)
        self.assertEqual(result["state"], "available")
        self.assertEqual((result["used_tokens"], result["window_tokens"]), (40500, 270000))
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (40000, 500))

    def test_history_files_and_database_unchanged(self):
        self.write(event())
        files = [self.path, self.home / "state_5.sqlite"]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        self.reader.lookup(self.packet, 103)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in files])

    def test_duplicate_native_notifications_are_not_two_responses(self):
        self.write(event(), event(at=102.5))
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "available")

    def test_ambiguous_responses_and_overlapping_requests_are_unavailable(self):
        self.write(event(), event(at=102.5, cumulative=1040499))
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "unavailable")
        self.write(event())
        self.assertEqual(self.reader.lookup(self.packet, 100.5)["state"], "unavailable")

    def test_next_request_and_model_boundaries(self):
        self.write(event(at=103))
        self.assertEqual(self.reader.lookup(self.packet, 102)["state"], "unavailable")
        self.packet["model"] = "different-model"
        self.assertEqual(self.reader.lookup(self.packet, 104)["state"], "unavailable")

    def test_compaction_estimates_and_failed_requests_are_not_actual_usage(self):
        estimate = event(inputs=0, outputs=0)
        estimate["payload"]["info"]["last_token_usage"]["total_tokens"] = 43000
        self.write(estimate)
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "unavailable")
        self.write(event())
        for field, value in (("is_compaction", True), ("status", 500), ("method", "GET")):
            self.assertEqual(self.reader.lookup({**self.packet, field: value}, 103)["state"], "unavailable")

    def test_partial_append_then_completed_event(self):
        self.write()
        raw = json.dumps(event())
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(raw[:70])
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "loading")
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(raw[70:] + "\n")
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "available")

    def test_bounded_reads_resume_without_losing_events(self):
        self.write(event())
        with patch("packet_inspector.token_window.READ_BUDGET", 50):
            results = [self.reader.lookup(self.packet, 103) for _ in range(8)]
        self.assertEqual(results[-1]["state"], "available")
        self.assertTrue(any(r["state"] == "loading" for r in results))

    def test_ready_lookup_reads_multiple_chunks_without_browser_polling(self):
        self.write(event())
        with patch("packet_inspector.token_window.READ_BUDGET", 50):
            result = self.reader.lookup_ready(self.packet, 103)
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["used_tokens"], 40500)

    def test_ready_lookup_keeps_a_bound_on_large_history_reads(self):
        self.write()
        with self.path.open("a", encoding="utf8") as stream:
            for _ in range(100):
                stream.write(json.dumps({"type": "response_item", "payload": "x" * 1000}) + "\n")
            stream.write(json.dumps(event()) + "\n")
        with patch("packet_inspector.token_window.READ_BUDGET", 50):
            self.assertEqual(self.reader.lookup_ready(self.packet, 103)["state"], "loading")
        self.assertEqual(self.reader.lookup_ready(self.packet, 103)["state"], "available")

    def test_ready_lookup_returns_while_native_writer_has_a_partial_line(self):
        self.write()
        raw = json.dumps(event())
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(raw[:70])
        self.assertEqual(self.reader.lookup_ready(self.packet, 103)["state"], "loading")
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(raw[70:] + "\n")
        self.assertEqual(self.reader.lookup_ready(self.packet, 103)["used_tokens"], 40500)

    def test_wrong_session_or_external_rollout_is_not_read(self):
        self.prefix[0]["payload"]["id"] = "other-session"
        self.write(event())
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "unavailable")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(self.home / "outside.jsonl"),))
            db.commit()
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "unavailable")

    def test_streaming_waits_for_native_usage(self):
        self.write()
        self.packet.pop("duration_ms")
        self.packet["state"] = "streaming"
        self.assertEqual(self.reader.lookup(self.packet)["state"], "pending")
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event()) + "\n")
        self.assertEqual(self.reader.lookup(self.packet)["state"], "available")

    def test_latest_window_tracks_native_estimates_and_new_response_counts(self):
        estimate = event(inputs=0, outputs=0)
        estimate["payload"]["info"]["last_token_usage"]["total_tokens"] = 43000
        self.write(event(at=101), estimate)
        value = self.reader.latest(THREAD)
        self.assertTrue(value["estimated"])
        self.assertEqual(value["used_tokens"], 43000)
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=103, inputs=45000)) + "\n")
        value = self.reader.latest(THREAD)
        self.assertEqual(value["used_tokens"], 45500)
        self.assertFalse(value["estimated"])

    def test_latest_window_skips_partial_last_line(self):
        self.write(event())
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=103, inputs=45000))[:80])
        self.assertEqual(self.reader.latest(THREAD)["used_tokens"], 40500)

    def test_latest_window_rejects_other_session_history(self):
        self.prefix[0]["payload"]["id"] = "other-session"
        self.write(event())
        self.assertEqual(self.reader.latest(THREAD)["state"], "unavailable")

    @unittest.skipUnless(os.name == "nt", "Windows rollout paths")
    def test_windows_extended_path_is_still_inside_codex_home(self):
        self.write(event())
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", ("\\\\?\\" + str(self.path),))
            db.commit()
        self.assertEqual(self.reader.lookup(self.packet, 103)["state"], "available")


if __name__ == "__main__":
    unittest.main()
