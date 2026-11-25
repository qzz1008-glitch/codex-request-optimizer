from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zlib

from packet_inspector.session_summary import SessionSummaries
from packet_inspector.token_window import TokenWindows
from packet_inspector.analyzer import inspect_request
from packet_inspector.request_context import INDEX_TASK_PREFIX, NEXT_MESSAGE_PREFIX, TITLE_REQUEST_PREFIX
from packet_inspector.storage import Store
from tests.test_token_window import THREAD, event


class SessionSummaryTests(unittest.TestCase):
    def test_new_partial_index_does_not_hide_threads_in_older_revisions(self):
        other = "01a1592b-c6ab-7ab1-8c9b-8e74da992bcc"
        for revision in range(6, 10):
            with closing(sqlite3.connect(self.native / f"state_{revision}.sqlite")) as db:
                db.execute("CREATE TABLE threads(id TEXT,name TEXT)")
                if revision == 9:
                    db.execute("INSERT INTO threads VALUES (?,?)", (other, "New schema thread"))
                db.commit()
        paths = list(self.native.glob("state_*.sqlite"))
        before = {path: path.read_bytes() for path in paths}
        labels = self.reader._labels([THREAD, other])
        self.assertEqual(labels[THREAD]["title"], "会话名称")
        self.assertEqual(labels[other]["title"], "New schema thread")
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_newer_row_with_cleared_title_does_not_resurrect_old_name(self):
        with closing(sqlite3.connect(self.native / "state_6.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT)")
            db.execute("INSERT INTO threads VALUES (?,NULL)", (THREAD,))
            db.commit()
        self.assertIsNone(self.reader._labels([THREAD])[THREAD]["title"])

    def test_conflicting_parent_evidence_during_index_migration_stays_unavailable(self):
        first = "01a1592b-c6ab-7ab1-8c9b-8e74da992bcc"
        second = "01a1592b-c6ab-7ab1-8c9b-8e74da992bcd"
        with closing(sqlite3.connect(self.native / "state_6.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT)")
            db.execute("CREATE TABLE thread_spawn_edges(child_thread_id TEXT,parent_thread_id TEXT)")
            db.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", (THREAD, first))
            db.commit()
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            db.execute("UPDATE threads SET source=?", (json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": second}}}),))
            db.commit()
        label = self.reader._labels([THREAD])[THREAD]
        self.assertEqual(label["title"], "会话名称")
        self.assertNotIn("parent_thread_id", label)

    def test_missing_title_and_timestamp_columns_do_not_hide_available_native_name(self):
        with closing(sqlite3.connect(self.native / "state_6.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT,name TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", (THREAD, "Name from a different schema"))
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "Name from a different schema")
        self.assertEqual(row["updated_at"], 105)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.native = self.root / "codex"
        self.native.mkdir()
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.rollout = self.native / "sessions" / "rollout-test.jsonl"
        self.rollout.parent.mkdir()
        self.rollout.write_text("".join(json.dumps(e) + "\n" for e in [
            {"type": "session_meta", "payload": {"id": THREAD}}, event()]), encoding="utf8")
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,updated_at_ms INTEGER,rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (THREAD, "会话名称", 110, 110500, str(self.rollout)))
            db.commit()
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("CREATE TABLE packet_summaries(id TEXT,created REAL,thread TEXT,method TEXT,meta TEXT)")
            db.executemany("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", [
                ("a", 100, THREAD, "POST", json.dumps({"outbound_wire_bytes": 1234})),
                ("b", 105, THREAD, "POST", json.dumps({"outbound_wire_bytes": 4000})),
                ("c", 120, THREAD, "GET", json.dumps({"outbound_wire_bytes": 9999})),
                ("d", 90, None, "POST", json.dumps({"wire_bytes": 8000}))])
            db.commit()
        self.reader = SessionSummaries(TokenWindows(self.native))

    def tearDown(self):
        self.temp.cleanup()

    def test_recent_packet_title_native_update_and_token_window(self):
        rows = self.reader.read(self.capture)["sessions"]
        self.assertEqual(rows[0]["title"], "会话名称")
        self.assertIsNone(rows[0]["origin"])
        self.assertEqual(rows[0]["updated_at"], 110.5)
        self.assertEqual(rows[0]["packet_count"], 2)
        self.assertEqual(rows[0]["latest_packet"], {"id": "b", "created": 105, "wire_bytes": 4000})
        self.assertEqual(rows[0]["token_window"]["used_tokens"], 40500)
        self.assertNotIn("window_tokens", rows[0]["token_window"])

    def test_unknown_session_does_not_use_uncompressed_bytes_as_upload_size(self):
        row = next(r for r in self.reader.read(self.capture)["sessions"] if not r["thread_id"])
        self.assertIsNone(row["latest_packet"]["wire_bytes"])
        self.assertIsNone(row["title"])
        self.assertEqual(row["token_window"]["state"], "unavailable")

    def test_latest_packet_ties_and_empty_ids_keep_counts_and_exclude_gets(self):
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.executemany("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", [
                ("named-a", 115, THREAD, "POST", json.dumps({"outbound_wire_bytes": 10})),
                ("named-z", 115, THREAD, "POST", json.dumps({"outbound_wire_bytes": 20})),
                ("unknown-a", 115, "", "POST", json.dumps({"outbound_wire_bytes": 30})),
                ("unknown-z", 115, None, "POST", json.dumps({"outbound_wire_bytes": 40})),
                ("unknown-get", 125, "", "GET", json.dumps({"outbound_wire_bytes": 50}))])
            db.commit()
        rows = {row["thread_id"]: row for row in self.reader.read(self.capture)["sessions"]}
        self.assertEqual(rows[THREAD]["packet_count"], 4)
        self.assertEqual(rows[THREAD]["latest_packet"], {"id": "named-z", "created": 115, "wire_bytes": 20})
        self.assertEqual(rows[""]["packet_count"], 3)
        self.assertEqual(rows[""]["latest_packet"], {"id": "unknown-z", "created": 115, "wire_bytes": 40})

    def test_many_sessions_do_not_rescan_newer_sessions_for_each_latest_packet(self):
        path = (self.capture / "packets.sqlite").resolve()
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE INDEX packet_summary_thread ON packet_summaries(thread,created DESC,id DESC)")
            db.executemany("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", [
                (f"bulk-{group:03}-{turn:03}", 200 + group * 64 + turn, f"bulk-session-{group}", "POST", "{}")
                for group in range(64) for turn in range(64)])
            db.commit()
        original_connect = sqlite3.connect
        steps = 0
        def progress():
            nonlocal steps
            steps += 1000
            return int(steps > 250000)
        def connect(database, *args, **kwargs):
            db = original_connect(database, *args, **kwargs)
            if str(database).startswith(path.as_uri()):
                # Bound SQLite work, not wall time: this remains deterministic
                # across slower machines and catches a scan per session.
                db.set_progress_handler(progress, 1000)
            return db
        with patch("packet_inspector.session_summary.sqlite3.connect", side_effect=connect):
            rows = {row["thread_id"]: row for row in self.reader.read(self.capture)["sessions"]}
        self.assertEqual(len(rows), 66)
        for group in range(64):
            row = rows[f"bulk-session-{group}"]
            self.assertEqual(row["packet_count"], 64)
            self.assertEqual(row["latest_packet"]["id"], f"bulk-{group:03}-063")

    def test_predictive_usage_does_not_replace_real_sidebar_usage(self):
        meta = {"status": 200, "state": "complete", "usage_override_applied": True,
                "upstream_usage": {"input_tokens": 123456, "output_tokens": 73, "total_tokens": 123529},
                "reported_usage": {"input_tokens": 500000, "output_tokens": 73, "total_tokens": 500073}}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps(meta),))
            db.commit()
        with patch.object(self.reader.tokens, "latest", side_effect=AssertionError("Native usage must not be read")):
            row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["token_window"]["input_tokens"], 123456)
        self.assertEqual(row["token_window"]["used_tokens"], 123529)
        self.assertNotIn("window_tokens", row["token_window"])
        self.assertEqual(row["token_window"]["source"], "upstream_response")

    def test_missing_real_sidebar_usage_never_falls_back_to_override(self):
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'",
                       (json.dumps({"usage_override_applied": True}),))
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["token_window"], {"state": "unavailable"})

    def test_tool_search_keeps_real_usage_of_the_predictive_request(self):
        with self.rollout.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=108, inputs=500000, outputs=73)) + "\n")
        generation = {"path": "/v1/responses", "status": 200, "state": "complete",
                      "usage_override_applied": True,
                      "upstream_usage": {"input_tokens": 123456, "output_tokens": 73, "total_tokens": 123529},
                      "reported_usage": {"input_tokens": 500000, "output_tokens": 73, "total_tokens": 500073}}
        search = {"path": "/v1/alpha/search", "request_kind": "tool_search", "outbound_wire_bytes": 850}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps(generation),))
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", ("search", 110, THREAD, "POST", json.dumps(search)))
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["latest_packet"], {"id": "search", "created": 110, "wire_bytes": 850})
        self.assertEqual(row["token_window"]["input_tokens"], 123456)
        self.assertEqual(row["token_window"]["recorded_at"], 105)

    def test_overflow_uses_original_estimate_even_when_prediction_also_applies(self):
        estimate = {"state": "available", "source": "codex_estimate", "estimated": True, "input_tokens": 260000}
        meta = {"path": "/v1/responses", "status": 200, "state": "complete", "usage_override_applied": True,
                "upstream_usage": {"input_tokens": 30000, "output_tokens": 2, "total_tokens": 30002},
                "reported_usage": {"input_tokens": 500000, "output_tokens": 2, "total_tokens": 500002}}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)",
                       ("search", 110, THREAD, "POST", '{"path":"/v1/alpha/search","request_kind":"tool_search"}'))
            db.commit()
            for protection in ({"overflow_compaction_calls": 1}, {"overflow_cache_hit": True}):
                for request_estimate in (estimate, {"state": "unavailable"}):
                    with self.subTest(protection=protection, estimate=request_estimate["state"]):
                        db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'",
                                   (json.dumps({**meta, **protection, "request_token_estimate": request_estimate}),))
                        db.commit()
                        row = self.reader.read(self.capture)["sessions"][0]
                        expected = {**estimate, "recorded_at": 105} if request_estimate is estimate else {"state": "unavailable"}
                        self.assertEqual(row["token_window"], expected)

    def test_compact_boundaries_do_not_inherit_predictions_and_real_500k_is_preserved(self):
        with self.rollout.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=108, inputs=500000, outputs=73)) + "\n")
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps({
                "path": "/v1/responses", "usage_override_applied": True,
                "reported_usage": {"input_tokens": 500000},
            }),))
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)",
                       ("compact", 109, THREAD, "POST", '{"path":"/v1/responses/compact","status":200,"state":"complete"}'))
            db.commit()
            self.assertEqual(self.reader.read(self.capture)["sessions"][0]["token_window"], {"state": "unavailable"})

            with self.rollout.open("a", encoding="utf8") as stream:
                stream.write(json.dumps(event(at=113, inputs=121000, outputs=3, cumulative=1500000)) + "\n")
            meta = {"path": "/v1/responses", "status": 200, "state": "complete",
                    "duration_ms": 2000, "usage_override_request_bytes": 980000}
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", ("next", 110, THREAD, "POST", json.dumps(meta)))
            db.commit()
            self.assertEqual(self.reader.read(self.capture)["sessions"][0]["token_window"], {"state": "unavailable"})

            meta["upstream_usage"] = {"input_tokens": 500000, "output_tokens": 3, "total_tokens": 500003}
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='next'", (json.dumps(meta),))
            db.commit()
        window = self.reader.read(self.capture)["sessions"][0]["token_window"]
        self.assertEqual((window["input_tokens"], window["source"]), (500000, "upstream_response"))

    def test_delayed_native_prediction_cannot_supply_the_next_generation(self):
        with self.rollout.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=113, inputs=500000, outputs=73)) + "\n")
        meta = {"path": "/v1/responses", "status": 200, "state": "complete", "duration_ms": 2000}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", ("next", 110, THREAD, "POST", json.dumps(meta)))
            db.commit()
            window = self.reader.read(self.capture)["sessions"][0]["token_window"]
            self.assertEqual((window["input_tokens"], window["source"]), (500000, "codex_history"))

            for previous in ({"usage_override_applied": True, "reported_usage": {"input_tokens": 500000}},
                             {"usage_override_applied": True}):
                with self.subTest(previous=previous):
                    db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps(previous),))
                    db.commit()
                    self.assertEqual(self.reader.read(self.capture)["sessions"][0]["token_window"], {"state": "unavailable"})

    def test_sidebar_uses_last_captured_tokens_until_new_usage_is_available(self):
        generation = {"path": "/v1/responses", "status": 200, "state": "complete"}
        prior = {**generation, "overflow_cache_hit": True, "request_token_estimate": {
            "state": "available", "source": "codex_estimate", "estimated": True, "input_tokens": 260000}}
        prediction = {**generation, "usage_override_applied": True,
                      "reported_usage": {"input_tokens": 500000, "output_tokens": 2, "total_tokens": 500002}}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps(prior),))
            db.executemany("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", [
                ("prediction", 108, THREAD, "POST", json.dumps(prediction)),
                ("next", 110, THREAD, "POST", json.dumps({**generation, "state": "streaming"})),
                ("other", 109, "other-session", "POST", json.dumps({**generation,
                    "upstream_usage": {"input_tokens": 700, "output_tokens": 2, "total_tokens": 702}})),
            ])
            db.commit()
            summary = self.reader.read(self.capture)
            row = next(row for row in summary["sessions"] if row["thread_id"] == THREAD)
            self.assertEqual(row["latest_packet"]["id"], "next")
            self.assertEqual(row["token_window"], {**prior["request_token_estimate"], "recorded_at": 105})

            generation["upstream_usage"] = {"input_tokens": 175000, "output_tokens": 3, "total_tokens": 175003}
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='next'", (json.dumps(generation),))
            db.commit()
        row = next(row for row in self.reader.read(self.capture)["sessions"] if row["thread_id"] == THREAD)
        self.assertEqual((row["token_window"]["input_tokens"], row["token_window"]["recorded_at"]), (175000, 110))

    def test_sidebar_keeps_previous_attributed_native_tokens_when_compact_has_no_usage(self):
        with self.rollout.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(event(at=107, inputs=127000, outputs=6)) + "\n")
        generation = {"path": "/v1/responses", "status": 200, "state": "complete", "duration_ms": 1000}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET meta=? WHERE id='b'", (json.dumps(generation),))
            db.commit()
            previous = self.reader.read(self.capture)["sessions"][0]["token_window"]
            self.assertEqual((previous["input_tokens"], previous["source"]), (127000, "codex_history"))
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)",
                       ("compact", 109, THREAD, "POST", '{"path":"/v1/responses/compact","status":200,"state":"complete"}'))
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["latest_packet"]["id"], "compact")
        self.assertEqual(row["token_window"], previous)

        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packet_summaries SET created=106 WHERE id='compact'")
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["token_window"], {"state": "unavailable"})

    def test_renames_and_updated_packet_values_refresh_without_history_writes(self):
        before = self.rollout.read_bytes(), self.rollout.stat().st_mtime_ns
        self.reader.read(self.capture)
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET title=?,updated_at_ms=?", ("更新后的名称", 121000))
            db.commit()
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("INSERT INTO packet_summaries VALUES (?,?,?,?,?)", ("e", 120, THREAD, "POST", '{"outbound_wire_bytes":1500}'))
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual((row["title"], row["updated_at"], row["latest_packet"]["wire_bytes"]), ("更新后的名称", 121, 1500))
        self.assertEqual(before, (self.rollout.read_bytes(), self.rollout.stat().st_mtime_ns))

    def test_missing_native_metadata_preserves_packet_summary(self):
        (self.native / "state_5.sqlite").unlink()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertIsNone(row["title"])
        self.assertIsNone(row["origin"])
        self.assertEqual(row["updated_at"], 105)
        self.assertEqual(row["latest_packet"]["wire_bytes"], 4000)

    def test_first_input_title_supports_older_details_and_yields_to_native_name(self):
        path = self.native / "state_5.sqlite"
        path.unlink()
        capture = self.root / "input-title-capture"
        store = Store(capture)

        def add(at, inputs, route="/responses"):
            meta, content = inspect_request(json.dumps({"input": inputs}).encode(), {"x-codex-thread-id": THREAD}, route)
            with patch("packet_inspector.storage.time.time", return_value=at):
                return store.add({**meta, "method": "POST", "path": route}, content)

        add(101, [{"role": "user", "content": "Summarize the context"}], "/responses/compact")
        add(102, [{"role": "user", "content": "<environment_context>\n" + "x" * 200 + "\n</environment_context>"}])
        first = add(103, [{"role": "developer", "content": "Instructions from the application"},
                          {"role": "user", "content": "# AGENTS.md instructions\n<INSTRUCTIONS>\nLocal rules\n</INSTRUCTIONS>"},
                          {"role": "user", "content": [{"type": "input_text", "text":
                           '<image name="sample" path="C:/sample.png"></image>\n  检查  \n sk-0123456789abcdefghijklm  ' + "文" * 50}]}])
        add(104, [{"role": "user", "content": "Later follow-up"}])
        prefix = "检查 [credential redacted] "
        title = prefix + "文" * (48 - len(prefix)) + "…"
        self.assertEqual(store.get(first)[0]["title"], title)
        with store.connect() as db:
            meta = json.loads(db.execute("SELECT meta FROM packets WHERE id=?", (first,)).fetchone()[0])
            meta.pop("title")
            meta["input_items"] = [None, "legacy metadata", {"kind": "reasoning", "label": "Hidden detail"},
                                   *meta["input_items"]]
            db.execute("UPDATE packets SET meta=? WHERE id=?", (json.dumps(meta), first))

        before = (capture / "packets.sqlite").read_bytes()
        with patch("packet_inspector.session_summary.zlib.decompressobj", side_effect=AssertionError("body read")):
            row = self.reader.read(capture)["sessions"][0]
        self.assertEqual(row["title"], title)
        self.assertEqual(row["title_source"], "input")
        self.assertNotIn("input_title", row)
        self.assertEqual((capture / "packets.sqlite").read_bytes(), before)

        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", (THREAD, "Native name"))
            db.commit()
        row = self.reader.read(capture)["sessions"][0]
        self.assertEqual(row["title"], "Native name")
        self.assertNotIn("title_source", row)

    def test_input_title_covers_older_sessions_after_details_are_pruned(self):
        older = "00000000-0000-0000-0000-000000000001"
        capture = self.root / "older-input-title-capture"
        store = Store(capture)
        body = json.dumps({"input": "First request"}).encode()
        route = "a" * 64
        origin = {"kind": "remote", "address": "198.51.100.176"}
        for at, thread in enumerate((older, THREAD), start=100):
            meta, content = inspect_request(body, {"x-codex-thread-id": thread}, "/responses")
            if thread == older:
                meta.update(source="live", ssh_connection_key=route)
            with patch("packet_inspector.storage.time.time", return_value=at):
                packet = store.add({**meta, "method": "POST", "path": "/responses"}, content)
            if thread == older:
                older_packet = packet
        with patch("packet_inspector.session_summary.MAX_SESSIONS", 1), \
             patch.object(self.reader, "ssh_origins", return_value={route: origin}) as resolve:
            sessions = self.reader.read(capture)["sessions"]
        resolve.assert_called_once_with({route})
        self.assertEqual(sessions[1]["origin"], origin)
        self.assertEqual([(row["thread_id"], row.get("title_source")) for row in sessions],
                         [(THREAD, None), (older, "input")])
        self.assertEqual(sessions[1]["title"], "First request")
        store.configure_limits(1_000_000, 10, 0)
        self.assertIsNone(store.get(older_packet, content=True)[1])
        saved = next(row for row in self.reader.read(capture)["sessions"] if row["thread_id"] == older)
        self.assertEqual(saved["title"], "First request")

    def test_conversations_keep_their_identity_without_originator_or_details(self):
        capture = self.root / "conversation-capture"
        store = Store(capture)
        standalone, legacy, title, compact, prewarm, terminal, summary_only, legacy_title = [
            f"00000000-0000-0000-0000-{number:012d}" for number in range(1, 9)]

        def add(thread, *, origin="codex_exec", path="/responses", context=None):
            headers = {"x-codex-thread-id": thread}
            if origin:
                headers["Originator"] = origin
            if context:
                headers["x-codex-turn-metadata"] = json.dumps(context)
            raw = b'{"input":[{"role":"user","content":"First request"}]}'
            meta, content = inspect_request(raw, headers, path)
            return store.add({**meta, "method": "POST", "path": path}, content)

        first = add(standalone)
        add(standalone, path="/responses/compact")
        previous = add(legacy)
        add(title, context={"thread_source": "thread_title"})
        add(compact, path="/responses/compact")
        add(prewarm, context={"request_kind": "prewarm"})
        add(terminal, origin="codex_cli_rs")
        pruned = add(summary_only, origin=None, context={"request_kind": "turn"})
        old_title = store.add({"thread_id": legacy_title, "method": "POST", "path": "/responses",
                               "input_items": [{"kind": "message:user", "label": TITLE_REQUEST_PREFIX}]})
        with store.connect() as db:
            db.execute("UPDATE packets SET meta=json_remove(meta,'$.title','$.headers') WHERE id=?", (previous,))
            db.execute("UPDATE packets SET meta=json_remove(meta,'$.request_purpose') WHERE id=?", (old_title,))
            db.execute("""UPDATE packets SET content=NULL,
                meta=json_set(json_remove(meta,'$.title','$.headers','$.input_items'),'$.details_evicted',1)
                WHERE id=?""", (pruned,))
        conversations = {standalone, legacy, terminal, summary_only}
        sessions = {row["thread_id"]: row for row in self.reader.read(capture)["sessions"]}
        self.assertEqual({thread for thread, row in sessions.items() if row.get("has_conversation")}, conversations)
        self.assertIsNone(sessions[summary_only]["title"])
        self.assertEqual(sessions[title]["session_kind"], "title_generation")
        store.configure_limits(1_000_000, 25, 0)
        saved, body = store.get(first, content=True)
        self.assertIsNone(body)
        self.assertNotIn("headers", saved)
        saved, body = store.get(previous, content=True)
        self.assertIsNone(body)
        self.assertNotIn("headers", saved)
        self.assertEqual(saved["title"], "First request")
        sessions = {row["thread_id"]: row for row in self.reader.read(capture)["sessions"]}
        self.assertEqual({thread for thread, row in sessions.items() if row.get("has_conversation")}, conversations)
        self.assertEqual(sessions[standalone]["title"], "First request")

    def test_sidebar_origin_uses_only_the_recorded_entry(self):
        capture = self.root / "session-origin"
        store = Store(capture)
        (self.native / ".codex-global-state.json").write_text(json.dumps({
            "thread-project-membership-host-ids": {THREAD: "remote-ssh-other"},
            "codex-managed-remote-connections": [{"hostId": "remote-ssh-other", "hostname": "192.0.2.50"}],
        }), encoding="utf8")
        ssh_origin = {"kind": "remote", "address": "198.51.100.176"}

        def add(at, source="live", **connection):
            with patch("packet_inspector.storage.time.time", return_value=at):
                store.add({"method": "POST", "path": "/responses", "thread_id": THREAD,
                           "source": source, **connection})

        with patch.object(self.reader, "ssh_origins", side_effect=lambda keys: {
                "known": ssh_origin} if "known" in keys else {}):
            def origin():
                return self.reader.read(capture)["sessions"][0]["origin"]

            add(100)
            self.assertIsNone(origin())
            add(101, source="imported", ssh_connection_key="known")
            self.assertIsNone(origin())
            add(102, ssh_connection_key="known")
            self.assertEqual(origin(), ssh_origin)
            add(103, wsl_connection_key="distribution")
            self.assertEqual(origin(), {"kind": "wsl"})
            add(104, local_connection=True)
            self.assertEqual(origin(), {"kind": "local"})
            add(105, ssh_connection_key="unknown")
            self.assertIsNone(origin())

    def test_catalog_reads_do_not_modify_databases(self):
        paths = [self.native / "state_5.sqlite", self.capture / "packets.sqlite"]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        self.reader.read(self.capture)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_empty_database_title_uses_latest_named_session_index_entry(self):
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET title='' ")
            db.commit()
        records = [{"id": THREAD, "thread_name": "旧名称", "updated_at": "1970-01-01T00:02:00Z"},
                   {"id": THREAD, "thread_name": "索引中的会话名称", "updated_at": "1970-01-01T00:03:00Z"}]
        (self.native / "session_index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf8")
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual((row["title"], row["updated_at"]), ("索引中的会话名称", 180))

    def subagent(self, spawn=None, name=None, nickname="Rawls"):
        spawn = spawn if spawn is not None else {"agent_path": "/root/core02_builder_continue", "agent_nickname": "Rawls"}
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            for column in ("name", "agent_nickname", "source"):
                db.execute("ALTER TABLE threads ADD COLUMN " + column + " TEXT")
            db.execute("UPDATE threads SET title='',name=?,agent_nickname=?,source=?",
                       (name, nickname, json.dumps({"subagent": {"thread_spawn": spawn}})))
            db.commit()

    def test_subagent_task_and_nickname_fill_empty_title_without_writes(self):
        self.subagent()
        paths = [self.native / "state_5.sqlite", self.rollout]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "子任务 · core02_builder_continue（Rawls）")
        self.assertEqual(row["session_kind"], "subagent")
        self.assertEqual(row["title_source"], "subagent")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_explicit_subagent_titles_override_generated_label(self):
        self.subagent()
        entry = {"id": THREAD, "thread_name": "索引中的子任务名称"}
        (self.native / "session_index.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf8")
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "索引中的子任务名称")
        self.assertEqual(row["session_kind"], "subagent")
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET title='正式子任务名称'")
            db.commit()
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "正式子任务名称")
        self.assertEqual(row["session_kind"], "subagent")
        self.assertNotIn("title_source", row)

    def test_subagent_native_name_and_nickname_only_sources(self):
        self.subagent({"agent_nickname": "Schrodinger"}, name="审计任务", nickname=None)
        self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "审计任务")
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET name=NULL")
            db.commit()
        self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "子任务 · Schrodinger")

    def test_subagent_source_works_without_optional_name_columns(self):
        source = {"subagent": {"thread_spawn": {"agent_path": "/root/audit", "agent_nickname": "Schrodinger"}}}
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            db.execute("UPDATE threads SET title='',source=?", (json.dumps(source),))
            db.commit()
        self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "子任务 · audit（Schrodinger）")

    def test_invalid_or_other_sources_do_not_invent_a_subagent_title(self):
        self.subagent()
        sources = [None, "invalid json", '"cli"', '[]', '{"subagent":"review"}',
                   '{"subagent":{"thread_spawn":{"agent_path":123,"agent_nickname":false}}}']
        for source in sources:
            with self.subTest(source=source):
                with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
                    db.execute("UPDATE threads SET source=?,agent_nickname=NULL", (source,))
                    db.commit()
                self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "")

    def index_request(self, *, remove_native=True, last_user=None, raw=None):
        text = (INDEX_TASK_PREFIX + " with a compact summary.\n"
                "This is a keyword retrieval index, not a broad prose summary.\n"
                "Current title: 排查压缩失败问题 (2)\nOnly fill the description field.")
        inputs = [{"role": "user", "content": [{"type": "input_text", "text": text}]}]
        if last_user is not None:
            inputs.append({"role": "user", "content": last_user})
        meta = json.dumps({"input_items": [{"kind": "message:user", "label": text[:140]}]})
        content = raw if raw is not None else zlib.compress(json.dumps({"body": {"input": inputs}}).encode())
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("CREATE TABLE packets(id TEXT,thread TEXT,meta TEXT,content BLOB)")
            db.execute("INSERT INTO packets VALUES (?,?,?,?)", ("b", THREAD, meta, content))
            db.commit()
        if remove_native:
            with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
                db.execute("DELETE FROM threads WHERE id=?", (THREAD,))
                db.commit()

    def test_ephemeral_index_request_recovers_explicit_source_title_without_writes(self):
        self.index_request()
        paths = [self.capture / "packets.sqlite", self.native / "state_5.sqlite", self.rollout]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        for _ in range(2):
            row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
            self.assertEqual(row["title"], "会话索引 · 排查压缩失败问题 (2)")
            self.assertEqual(row["title_source"], "index_task")
            self.assertEqual(row["session_kind"], "session_index")
            self.assertEqual(row["source_title"], "排查压缩失败问题 (2)")
            self.assertEqual(row["token_window"]["state"], "unavailable")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_ephemeral_compaction_recovers_parent_without_changing_history(self):
        parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
        self.index_request(last_user="Create a context checkpoint")
        body = {"input": [{"role": "user", "content": "Create a context checkpoint"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "thread_source": "thread_description", "forked_from_thread_id": parent})}}
        meta = {"is_compaction": True, "request_kind": "compaction",
                "top_fields": [{"name": "client_metadata"}]}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packets SET meta=?,content=?", (json.dumps(meta), zlib.compress(json.dumps({"body": body}).encode())))
            db.commit()
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (parent, "真实父会话", 120, 120000, None))
            db.commit()
        files = [self.capture / "packets.sqlite", self.native / "state_5.sqlite", self.rollout]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        rows = {r["thread_id"]: r for r in self.reader.read(self.capture)["sessions"]}
        self.assertEqual(rows[THREAD]["parent_thread_id"], parent)
        self.assertEqual(rows[THREAD]["session_kind"], "session_index")
        self.assertIsNone(rows[THREAD]["title"])
        self.assertEqual(rows[parent]["title"], "真实父会话")
        self.assertEqual(rows[parent]["packet_count"], 0)
        self.assertIsNone(rows[parent]["latest_packet"])
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in files])

    def test_native_spawn_edges_recover_missing_parent_and_bound_cycles(self):
        parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT)")
            db.executemany("INSERT INTO thread_spawn_edges VALUES (?,?,?)", [(parent, THREAD, "complete"), (THREAD, parent, "complete")])
            db.commit()
        rows = {r["thread_id"]: r for r in self.reader.read(self.capture)["sessions"]}
        self.assertEqual(rows[THREAD]["parent_thread_id"], parent)
        self.assertEqual(rows[parent]["parent_thread_id"], THREAD)
        self.assertEqual(rows[THREAD]["session_kind"], "subagent")
        self.assertEqual(len(rows), 3)

    def test_real_title_has_priority_over_request_index_label(self):
        self.index_request(remove_native=False)
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "会话名称")
        self.assertNotIn("title_source", row)

    def test_index_prompt_in_older_context_does_not_label_a_regular_request(self):
        self.index_request(last_user="Continue the actual task. Current title: not a native indexing request")
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertIsNone(row["title"])

    def test_corrupt_capture_keeps_name_unavailable(self):
        self.index_request(raw=b"not a zlib stream")
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertIsNone(row["title"])

    def image_request(self, *, remove_native=True, text=None):
        analysis = text is None
        text = text or '<image name=[Image #1] path="C:\\example.png">\n</image>\nDescribe the attachment.'
        body = {"input": [{"role": "user", "content": text}]}
        if analysis:
            body["client_metadata"] = {"x-codex-turn-metadata": json.dumps({
                "codex_inspector_request_kind": "image_analysis"})}
        self.index_request(remove_native=remove_native, raw=zlib.compress(json.dumps({"body": body}).encode()))
        meta = {"input_items": [{"kind": "message:user", "label": text[:140]}]}
        if analysis:
            meta["top_fields"] = [{"name": "client_metadata"}]
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packets SET meta=?", (json.dumps(meta),))
            db.commit()

    def test_temporary_image_task_has_kind_without_inventing_native_title(self):
        self.image_request()
        paths = [self.capture / "packets.sqlite", self.rollout]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertIsNone(row["title"])
        self.assertEqual(row["session_kind"], "image_analysis")
        self.assertNotIn("title_source", row)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_native_title_has_priority_over_temporary_task_kind(self):
        self.image_request(remove_native=False)
        row = self.reader.read(self.capture)["sessions"][0]
        self.assertEqual(row["title"], "会话名称")
        self.assertNotIn("session_kind", row)

    def test_image_parent_survives_metadata_only_retention_without_native_child_history(self):
        child = "00000000-0000-0000-0000-000000000001"
        capture = self.root / "image-capture"
        store = Store(capture, max_count=10, max_detail_count=0)
        body = {"input": [{"role": "user", "content": "Describe the attachment."}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "codex_inspector_request_kind": "image_analysis", "codex_inspector_parent_thread_id": THREAD})}}
        meta, content = inspect_request(json.dumps(body).encode(), {"x-codex-thread-id": child}, "/responses")
        packet_id = store.add({**meta, "method": "POST"}, content)
        saved, content = store.get(packet_id, content=True)
        self.assertTrue(saved["details_evicted"])
        self.assertIsNone(content)
        self.assertEqual(saved["parent_thread_id"], THREAD)
        sessions = {row["thread_id"]: row for row in self.reader.read(capture)["sessions"]}
        self.assertEqual(sessions[child]["parent_thread_id"], THREAD)
        self.assertEqual(sessions[child]["session_kind"], "image_analysis")
        self.assertIsNone(sessions[child]["title"])
        self.assertEqual(sessions[THREAD]["title"], "会话名称")
        self.assertEqual(sessions[THREAD]["packet_count"], 0)

    def test_recommendation_purpose_survives_metadata_only_retention(self):
        child = "00000000-0000-0000-0000-000000000001"
        capture = self.root / "recommendation-capture"
        store = Store(capture, max_detail_count=0)
        body = {"input": [{"type": "custom_tool_call_output", "output": "repository details"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "request_kind": "turn", "thread_source": "ambient_suggestions"})}}
        meta, content = inspect_request(json.dumps(body).encode(), {"x-codex-thread-id": child}, "/responses")
        packet_id = store.add({**meta, "method": "POST"}, content)
        self.assertIsNone(store.get(packet_id, content=True)[1])
        row = self.reader.read(capture)["sessions"][0]
        self.assertEqual(row["thread_id"], child)
        self.assertEqual(row["session_kind"], "ambient_suggestions")
        self.assertIsNone(row["title"])
        self.assertNotIn("parent_thread_id", row)

    def test_title_purpose_survives_metadata_only_retention_without_inventing_a_parent(self):
        child = "00000000-0000-0000-0000-000000000001"
        capture = self.root / "title-capture"
        store = Store(capture, max_detail_count=0)
        body = {"input": [{"role": "user", "content": "用新版提示生成标题。"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "thread_source": "thread_title"})}}
        meta, content = inspect_request(json.dumps(body).encode(), {"x-codex-thread-id": child}, "/responses")
        packet_id = store.add({**meta, "method": "POST"}, content)
        self.assertIsNone(store.get(packet_id, content=True)[1])
        rows = {r["thread_id"]: r for r in self.reader.read(capture)["sessions"]}
        self.assertEqual(rows[child]["session_kind"], "title_generation")
        self.assertNotIn("parent_thread_id", rows[child])
        self.assertIsNone(rows[child]["title"])
        self.assertEqual(set(rows), {child})

    def test_legacy_title_dispatch_keeps_parent_unavailable_without_rewriting_capture(self):
        self.index_request()
        body = {"input": [{"role": "user", "content": "用新版提示生成标题。"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "thread_source": "thread_title"})}}
        meta = {"request_kind": "turn", "top_fields": [{"name": "client_metadata"}]}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packets SET meta=?,content=?", (json.dumps(meta), zlib.compress(json.dumps({"body": body}).encode())))
            db.commit()
        before = (self.capture / "packets.sqlite").read_bytes()
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertEqual(row["session_kind"], "title_generation")
        self.assertNotIn("parent_thread_id", row)
        self.assertIsNone(row["title"])
        self.assertEqual((self.capture / "packets.sqlite").read_bytes(), before)

    def test_legacy_recommendation_session_reads_explicit_source_without_inventing_a_title(self):
        self.index_request()
        body = {"input": [{"type": "custom_tool_call_output", "output": "repository details"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({"thread_source": "ambient_suggestions"})}}
        meta = {"request_kind": "turn", "top_fields": [{"name": "client_metadata"}]}
        with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
            db.execute("UPDATE packets SET meta=?,content=?", (json.dumps(meta), zlib.compress(json.dumps({"body": body}).encode())))
            db.commit()
        before = (self.capture / "packets.sqlite").read_bytes()
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertEqual(row["session_kind"], "ambient_suggestions")
        self.assertIsNone(row["title"])
        self.assertNotIn("parent_thread_id", row)
        self.assertEqual((self.capture / "packets.sqlite").read_bytes(), before)

    def test_ordinary_image_request_uses_user_text_after_the_attachment(self):
        self.image_request(text='<image name=[Image #1] path="C:\\example.png">\n</image>\n帮我看看这张图')
        row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
        self.assertEqual(row["title"], "帮我看看这张图")
        self.assertEqual(row["title_source"], "input")
        self.assertNotIn("session_kind", row)

    def test_auxiliary_metadata_is_classified_without_decompressing_body(self):
        self.image_request(text=NEXT_MESSAGE_PREFIX)
        for prefix, kind in ((NEXT_MESSAGE_PREFIX, "next_message_suggestion"),
                             (TITLE_REQUEST_PREFIX, "title_generation")):
            with self.subTest(kind=kind):
                self.reader._request_labels.clear()
                with closing(sqlite3.connect(self.capture / "packets.sqlite")) as db:
                    db.execute("UPDATE packets SET content=?,meta=?", (b"unreadable", json.dumps({"input_items": [
                        {"kind": "message:user", "label": prefix[:140]}]})))
                    db.commit()
                with patch("packet_inspector.session_summary.zlib.decompressobj", side_effect=AssertionError("body read")):
                    row = next(r for r in self.reader.read(self.capture)["sessions"] if r["thread_id"] == THREAD)
                self.assertIsNone(row["title"])
                self.assertEqual(row["session_kind"], kind)

if __name__ == "__main__":
    unittest.main()
