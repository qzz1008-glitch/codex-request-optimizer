"""Keep genuine usage separate from explicitly labelled pre-summary estimates."""
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

    def test_uses_single_response_usage_without_window_limits(self):
        self.write(event())
        result = self.reader.lookup(self.packet, 103)
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["used_tokens"], 40500)
        self.assertNotIn("window_tokens", result)
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (40000, 500))

    def test_real_response_usage_needs_no_native_files_or_database_reads(self):
        packet = {**self.packet, "upstream_usage": {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130}}
        with patch.object(self.reader, "lookup", side_effect=AssertionError("Native lookup must not run")):
            result = self.reader.lookup_ready(packet)
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (123, 7))
        self.assertNotIn("window_tokens", result)
        self.assertNotIn("window_pending", result)

    def test_new_database_without_rollout_column_does_not_hide_older_usage(self):
        self.write(event())
        with closing(sqlite3.connect(self.home / "state_6.sqlite")) as db:
            db.execute("CREATE TABLE threads (id TEXT)")
        self.assertEqual(self.reader.lookup(self.packet, 103)["input_tokens"], 40000)

    def test_more_than_three_database_revisions_do_not_hide_native_usage(self):
        self.write(event())
        for revision in range(6, 10):
            with closing(sqlite3.connect(self.home / f"state_{revision}.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT)")
        self.assertEqual(self.reader.lookup(self.packet, 103)["input_tokens"], 40000)
        self.assertEqual(self.reader.latest(THREAD)["input_tokens"], 40000)

    def test_subagent_usage_survives_inherited_parent_headers(self):
        self.prefix.insert(1, {"type": "session_meta", "payload": {"id": "parent-session"}})
        self.write(event())
        result = self.reader.lookup(self.packet, 103)
        self.assertEqual(result["state"], "available")
        self.assertEqual((result["input_tokens"], result["used_tokens"]), (40000, 40500))
        self.assertEqual(self.reader.latest(THREAD)["used_tokens"], result["used_tokens"])

    def test_inherited_header_cannot_validate_wrong_file_owner(self):
        self.prefix[0]["payload"]["id"] = "other-session"
        self.prefix.insert(1, {"type": "session_meta", "payload": {"id": THREAD}})
        self.write(event())
        self.assertEqual(self.reader.lookup(self.packet, 103), {"state": "unavailable"})

    def test_subagent_file_owner_survives_resumed_bounded_reads(self):
        self.prefix.insert(1, {"type": "session_meta", "payload": {"id": "parent-session"}})
        self.write(event())
        with patch("packet_inspector.token_window.READ_BUDGET", 50):
            results = [self.reader.lookup(self.packet, 103) for _ in range(8)]
        self.assertTrue(any(result["state"] == "loading" for result in results))
        self.assertEqual(results[-1]["state"], "available")
        self.assertEqual(results[-1]["input_tokens"], 40000)

    def test_history_files_and_database_unchanged(self):
        self.write(event())
        files = [self.path, self.home / "state_5.sqlite"]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        self.reader.lookup(self.packet, 103)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in files])

    def test_tool_search_cannot_claim_an_adjacent_predictive_usage_event(self):
        self.write(event(inputs=500000, outputs=256))
        for path in ("/alpha/search", "/v1/alpha/search/?query=tool", "/other-api"):
            packet = {**self.packet, "path": path}
            with self.subTest(path=path), patch.object(self.reader, "_rollout", side_effect=AssertionError("not a model request")):
                self.assertEqual(self.reader.lookup(packet, 103), {"state": "unavailable"})
                self.assertEqual(self.reader.lookup_ready(packet, 103), {"state": "unavailable"})
                packet["upstream_usage"] = {"input_tokens": 500000, "output_tokens": 256, "total_tokens": 500256}
                self.assertEqual(self.reader.lookup_ready(packet, 103), {"state": "unavailable"})

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

    def test_delayed_native_usage_matches_before_the_next_request(self):
        self.write(event(at=112.8, inputs=45000))
        result = self.reader.lookup(self.packet, 113)
        self.assertEqual(result["state"], "available")
        self.assertEqual((result["input_tokens"], result["recorded_at"]), (45000, 112.8))

    def test_completed_request_keeps_waiting_for_delayed_history(self):
        self.write()
        with patch("packet_inspector.token_window.time.time", return_value=110):
            self.assertEqual(self.reader.lookup(self.packet), {"state": "pending"})
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=112.8, inputs=45000)) + "\n")
        with patch("packet_inspector.token_window.time.time", return_value=113):
            self.assertEqual(self.reader.lookup(self.packet)["input_tokens"], 45000)
        self.write()
        with patch("packet_inspector.token_window.time.time", return_value=162):
            self.assertEqual(self.reader.lookup(self.packet), {"state": "unavailable"})

    def test_late_usage_never_crosses_the_next_request_boundary(self):
        self.write(event(at=113))
        self.assertEqual(self.reader.lookup(self.packet, 113), {"state": "unavailable"})
        self.assertEqual(self.reader.lookup(self.packet, 114)["state"], "available")

    def test_ambiguous_delayed_usage_is_not_assigned(self):
        self.write(event(at=111), event(at=112, cumulative=1040499))
        self.assertEqual(self.reader.lookup(self.packet, 113), {"state": "unavailable"})

    def test_late_usage_has_a_bounded_settle_period(self):
        self.write(event(at=162))
        self.assertEqual(self.reader.lookup(self.packet, 200), {"state": "unavailable"})

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
