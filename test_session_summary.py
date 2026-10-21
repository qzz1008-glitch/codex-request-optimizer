from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from packet_inspector.session_summary import INDEX_TASK_PREFIX, SessionSummaries
from packet_inspector.token_window import TokenWindows
from test_token_window import THREAD, event


class SessionSummaryTests(unittest.TestCase):
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
        self.assertEqual(rows[0]["updated_at"], 110.5)
        self.assertEqual(rows[0]["packet_count"], 2)
        self.assertEqual(rows[0]["latest_packet"], {"id": "b", "created": 105, "wire_bytes": 4000})
        self.assertEqual(rows[0]["token_window"]["used_tokens"], 40500)
        self.assertEqual(rows[0]["token_window"]["window_tokens"], 270000)

    def test_unknown_session_does_not_use_uncompressed_bytes_as_upload_size(self):
        row = next(r for r in self.reader.read(self.capture)["sessions"] if not r["thread_id"])
        self.assertIsNone(row["latest_packet"]["wire_bytes"])
        self.assertIsNone(row["title"])
        self.assertEqual(row["token_window"]["state"], "unavailable")

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
        self.assertEqual(row["updated_at"], 105)
        self.assertEqual(row["latest_packet"]["wire_bytes"], 4000)

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
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_explicit_subagent_titles_override_generated_label(self):
        self.subagent()
        entry = {"id": THREAD, "thread_name": "索引中的子任务名称"}
        (self.native / "session_index.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf8")
        self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "索引中的子任务名称")
        with closing(sqlite3.connect(self.native / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET title='正式子任务名称'")
            db.commit()
        self.assertEqual(self.reader.read(self.capture)["sessions"][0]["title"], "正式子任务名称")

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
            self.assertEqual(row["source_title"], "排查压缩失败问题 (2)")
            self.assertEqual(row["token_window"]["state"], "unavailable")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

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


if __name__ == "__main__":
    unittest.main()
