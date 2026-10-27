"""Local bounded storage. Only sanitized viewing copies are written to disk."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import zlib

from .analyzer import CLASSIFICATION_VERSION, classify_request, encode, compare_packets
from .maintenance import SQLiteReclaimer
from .request_context import packet_metadata, trend_packet_metadata

SUMMARY_COLUMNS = "id,created,thread,status,method,kind,status_key,state,capture_owner,wire_bytes,stored_bytes,meta"
COMPRESSED_SIZE_SQL = "coalesce(json_extract(meta,'$.overflow_original_wire_bytes'),json_extract(meta,'$.usage_override_request_bytes'),json_extract(meta,'$.outbound_wire_bytes'),-1)"
PENDING_CLASSIFICATION = f"coalesce(json_extract(meta,'$.classification_version'),0)<{CLASSIFICATION_VERSION}"
RETAINED_DETAILS = "coalesce(json_extract(meta,'$.details_evicted'),0)=0"
DETAIL_META_FIELDS = {"headers", "input_items"}
DEFAULT_STORAGE_BYTES = 500_000_000


def summary_values(row):
    return f"""{row}.id,{row}.created,{row}.thread,{row}.status,
        upper(coalesce(json_extract({row}.meta,'$.method'),'')),
        CASE WHEN json_extract({row}.meta,'$.is_compaction') THEN 'compaction'
             WHEN json_extract({row}.meta,'$.request_kind')='tool_search' THEN 'other'
             WHEN upper(coalesce(json_extract({row}.meta,'$.method'),''))='POST' THEN 'generation' ELSE 'other' END,
        CASE WHEN coalesce({row}.status,0)>0 THEN 'http:'||{row}.status
             ELSE 'state:'||coalesce(json_extract({row}.meta,'$.state'),'unknown') END,
        coalesce(json_extract({row}.meta,'$.state'),'unknown'),
        coalesce(json_extract({row}.meta,'$.capture_owner'),'relay'),
        coalesce(json_extract({row}.meta,'$.wire_bytes'),-1),
        coalesce(length({row}.content),0)+length(cast({row}.meta AS BLOB)),
        json_remove({row}.meta,'$.input_items','$.headers','$.top_fields','$.input_groups')"""


class Store:
    def __init__(self, directory: Path, max_bytes=DEFAULT_STORAGE_BYTES, max_count=2000,
                 capture_owner="relay", timeout=10, max_detail_count=None):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "packets.sqlite"
        self.max_bytes = max_bytes
        self.max_count = max_count
        self.max_detail_count = max_detail_count
        self.capture_owner = capture_owner
        self.timeout = timeout
        self.lock = threading.RLock()
        self.reclaimer = SQLiteReclaimer(self.path, self.lock)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS packets (
                id TEXT PRIMARY KEY, created REAL NOT NULL, thread TEXT NOT NULL,
                status INTEGER, fingerprint TEXT, meta TEXT NOT NULL, content BLOB)""")
            c.execute("CREATE INDEX IF NOT EXISTS packets_created ON packets(created DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS packets_thread ON packets(thread, created DESC)")
            c.execute(f"CREATE INDEX IF NOT EXISTS packets_retained_details ON packets(created,id) WHERE {RETAINED_DETAILS}")
            self._init_summaries(c)
            # Other relay workers may still be streaming during an upgrade.
            self._recover_owner(c, self.capture_owner)
            # The retired observer has no writer left to finish its records.
            if self.capture_owner != "existing_exit":
                self._recover_owner(c, "existing_exit")
            self._prune(c)

    @staticmethod
    def _recover_owner(c, owner):
        for row in c.execute("SELECT p.id,p.meta FROM packet_summaries s JOIN packets p ON p.id=s.id "
                             "WHERE s.state IN ('captured','streaming') AND s.capture_owner=?", (owner,)).fetchall():
            meta = json.loads(row["meta"])
            meta["state"] = "interrupted"
            c.execute("UPDATE packets SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), row["id"]))

    def recover_owner(self, owner):
        with self.lock, self.connect() as c:
            self._recover_owner(c, owner)

    def _init_summaries(self, c):
        # Keep the seven-column packets table compatible with already-running
        # observers. Database triggers also index writes made by older binaries.
        c.execute("BEGIN IMMEDIATE")
        c.execute("""CREATE TABLE IF NOT EXISTS packet_summaries (
            id TEXT PRIMARY KEY, created REAL NOT NULL, thread TEXT NOT NULL,
            status INTEGER, method TEXT NOT NULL, kind TEXT NOT NULL,
            status_key TEXT NOT NULL, state TEXT NOT NULL, capture_owner TEXT NOT NULL,
            wire_bytes INTEGER NOT NULL, stored_bytes INTEGER NOT NULL, meta TEXT NOT NULL)""")
        c.execute("CREATE TABLE IF NOT EXISTS packet_index_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)")
        c.execute("INSERT OR IGNORE INTO packet_index_state VALUES(1,0)")
        for event in ("INSERT", "UPDATE"):
            remove_old = "DELETE FROM packet_summaries WHERE id=OLD.id;" if event == "UPDATE" else ""
            c.execute(f"DROP TRIGGER IF EXISTS packet_summary_{event.lower()}")
            c.execute(f"""CREATE TRIGGER packet_summary_{event.lower()} AFTER {event} ON packets BEGIN
                {remove_old}
                INSERT OR REPLACE INTO packet_summaries ({SUMMARY_COLUMNS}) VALUES({summary_values('NEW')});
                UPDATE packet_index_state SET revision=revision+1 WHERE singleton=1;
                END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS packet_summary_delete AFTER DELETE ON packets BEGIN
            DELETE FROM packet_summaries WHERE id=OLD.id;
            UPDATE packet_index_state SET revision=revision+1 WHERE singleton=1;
            END""")
        for name, columns in {
            "created": "created DESC,id DESC",
            "size": "wire_bytes DESC,created DESC,id DESC",
            "upload_size": "coalesce(json_extract(meta,'$.outbound_wire_bytes'),-1) DESC,created DESC,id DESC",
            "compressed_size": f"{COMPRESSED_SIZE_SQL} DESC,created DESC,id DESC",
            "thread": "thread,created DESC,id DESC",
            "kind": "kind,created DESC,id DESC",
            "status": "status_key,created DESC,id DESC",
        }.items():
            c.execute(f"CREATE INDEX IF NOT EXISTS packet_summary_{name} ON packet_summaries({columns})")
        c.execute(f"CREATE INDEX IF NOT EXISTS packet_summary_classification_v{CLASSIFICATION_VERSION} "
                  f"ON packet_summaries(created DESC,id DESC) WHERE {PENDING_CLASSIFICATION}")
        added = c.execute(f"""INSERT INTO packet_summaries ({SUMMARY_COLUMNS})
            SELECT {summary_values('p')} FROM packets p
            WHERE NOT EXISTS (SELECT 1 FROM packet_summaries s WHERE s.id=p.id)""").rowcount
        if added:
            c.execute("UPDATE packet_index_state SET revision=revision+1 WHERE singleton=1")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=self.timeout)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def add(self, meta, content=None):
        packet_id = uuid.uuid4().hex
        created = time.time()
        meta = {**meta, "id": packet_id, "created": created,
                "time": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                "state": "captured", "source": meta.get("source", "live"),
                "capture_owner": self.capture_owner}
        meta = packet_metadata(meta)
        if self.max_detail_count == 0:
            meta = self._basic_metadata(meta)
            content = None
        blob = zlib.compress(encode(content), 3) if content is not None else None
        with self.lock, self.connect() as c:
            c.execute("INSERT INTO packets VALUES(?,?,?,?,?,?,?)",
                      (packet_id, created, meta.get("thread_id", ""), meta.get("status"),
                       meta.get("sha256", ""), json.dumps(meta, ensure_ascii=False), blob))
            self._prune(c)
        return packet_id

    @staticmethod
    def _basic_metadata(meta):
        return {**{key: value for key, value in meta.items() if key not in DETAIL_META_FIELDS},
                "details_evicted": True}

    def _discard_details(self, c, row):
        meta = json.dumps(self._basic_metadata(json.loads(row["meta"])), ensure_ascii=False)
        c.execute("UPDATE packets SET meta=?,content=NULL WHERE id=?", (meta, row["id"]))
        saved = row["stored_bytes"] - len(meta.encode("utf-8"))
        if saved > 0:
            self.reclaimer.request()
        return saved

    def configure_limits(self, max_bytes, max_count, max_detail_count=None):
        with self.lock:
            previous = self.max_bytes, self.max_count, self.max_detail_count
            self.max_bytes, self.max_count, self.max_detail_count = max_bytes, max_count, max_detail_count
            try:
                with self.connect() as c:
                    c.execute("BEGIN IMMEDIATE")
                    self._prune(c)
            except Exception:
                self.max_bytes, self.max_count, self.max_detail_count = previous
                raise

    def _prune(self, c):
        count, total = c.execute("SELECT count(*),coalesce(sum(stored_bytes),0) FROM packet_summaries").fetchone()
        if count > self.max_count:
            old = c.execute("SELECT id,stored_bytes FROM packet_summaries ORDER BY created DESC,id DESC LIMIT -1 OFFSET ?",
                            (max(0, self.max_count),)).fetchall()
            c.executemany("DELETE FROM packets WHERE id=?", [(row["id"],) for row in old])
            self.reclaimer.request()
            count -= len(old)
            total -= sum(row["stored_bytes"] for row in old)
        detail_limit = max(0, min(self.max_count, self.max_count if self.max_detail_count is None else self.max_detail_count))
        boundary = (c.execute("SELECT created,id FROM packet_summaries ORDER BY created DESC,id DESC LIMIT 1 OFFSET ?",
                              (detail_limit,)).fetchone() if count > detail_limit else None)
        detail_rows = f"SELECT id,meta,coalesce(length(content),0)+length(cast(meta AS BLOB)) AS stored_bytes FROM packets WHERE {RETAINED_DETAILS}"
        if boundary:
            for row in c.execute(detail_rows + " AND (created,id)<=(?,?) ORDER BY created,id", tuple(boundary)):
                total -= self._discard_details(c, row)
        # Keep list, chart and size statistics before sacrificing a basic record.
        while total > self.max_bytes:
            row = c.execute(detail_rows + " ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                break
            total -= self._discard_details(c, row)
        while total > self.max_bytes:
            row = c.execute("SELECT id,stored_bytes AS size FROM packet_summaries ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                break
            c.execute("DELETE FROM packets WHERE id=?", (row["id"],))
            self.reclaimer.request()
            total -= row["size"] or 0

    def update(self, packet_id, **fields):
        with self.lock, self.connect() as c:
            # Both the relay and the existing exit can update this record.
            # Lock before reading so neither writer overwrites the other's fields.
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT meta FROM packets WHERE id=?", (packet_id,)).fetchone()
            if row:
                meta = json.loads(row["meta"])
                if meta.get("details_evicted"):
                    fields = {key: value for key, value in fields.items()
                              if key not in DETAIL_META_FIELDS | {"details_evicted"}}
                meta.update(fields)
                c.execute("UPDATE packets SET status=?,meta=? WHERE id=?",
                          (meta.get("status"), json.dumps(meta, ensure_ascii=False), packet_id))
                self._prune(c)

    def get(self, packet_id, content=False):
        with self.connect() as c:
            row = c.execute("SELECT meta" + (",content" if content else "") + " FROM packets WHERE id=?", (packet_id,)).fetchone()
        if row is None:
            raise KeyError("packet_not_found")
        meta = packet_metadata(json.loads(row["meta"]))
        body = json.loads(zlib.decompress(row["content"])) if content and row["content"] else None
        return meta, body

    def reclassify_pending(self, limit=8):
        with self.connect() as c:
            rows = c.execute(f"""SELECT p.id,p.meta,p.content FROM
                (SELECT id FROM packet_summaries WHERE {PENDING_CLASSIFICATION}
                 ORDER BY created DESC,id DESC LIMIT ?) s JOIN packets p ON p.id=s.id""", (limit,)).fetchall()
        for row in rows:
            meta = json.loads(row["meta"])
            fields = {"classification_version": CLASSIFICATION_VERSION}
            if row["content"]:
                try:
                    view = json.loads(zlib.decompress(row["content"]))
                    fields.update(classify_request(view.get("body"), meta.get("headers"), meta.get("path", "")))
                except (ValueError, zlib.error, AttributeError) as exc:
                    fields["classification_error"] = type(exc).__name__
            elif meta.get("path", "").split("?", 1)[0].rstrip("/").endswith(("/compact", "/alpha/search")):
                fields.update(classify_request(None, path=meta["path"]))
            # update() merges under a write lock, retaining concurrent response
            # sizes, statuses and progress written by the old exit process.
            self.update(row["id"], **fields)
        return len(rows)

    def list(self, limit=200, thread="", query="", before=None, include_get=False,
             sort="created", direction="desc", after=None, kind="", status="",
             thread_unknown=False, since=None, trend=False):
        sort_fields = {
            "created": "created",
            "wire_bytes": "wire_bytes",
            "upload_bytes": "coalesce(json_extract(meta,'$.outbound_wire_bytes'),-1)",
            "compressed_bytes": COMPRESSED_SIZE_SQL,
            "decoded_bytes": "coalesce(json_extract(meta,'$.decoded_bytes'),-1)",
            "kind": "CASE WHEN json_extract(meta,'$.is_compaction') THEN '上下文压缩' WHEN json_extract(meta,'$.request_kind')='tool_search' THEN '工具搜索' WHEN json_extract(meta,'$.method')='POST' THEN '对话生成' ELSE coalesce(json_extract(meta,'$.method'),'未知') END",
            "status": "coalesce(status,-1)",
            "thread": "thread",
        }
        if sort not in sort_fields or direction not in {"asc", "desc"}:
            raise ValueError("invalid packet sort")
        if kind and kind not in {"generation", "compaction", "other"}:
            raise ValueError("invalid packet type filter")
        if status and not re.fullmatch(r"http:[1-5]\d{2}|state:[a-z_]{1,40}", status):
            raise ValueError("invalid packet status filter")
        if thread and thread_unknown:
            raise ValueError("conflicting thread filters")
        field = sort_fields[sort]
        order = "ASC" if direction == "asc" else "DESC"
        where, args = [], []
        if not include_get:
            where.append("method!='GET'")
        if thread:
            where.append("thread=?")
            args.append(thread)
        if thread_unknown:
            where.append("coalesce(thread,'')=''")
        if kind:
            where.append("kind=?")
            args.append(kind)
        if status:
            where.append("status_key=?")
            args.append(status)
        if query:
            where.append("(thread LIKE ? OR id IN (SELECT id FROM packets WHERE meta LIKE ?))")
            args.extend(["%" + query + "%"] * 2)
        if before is not None:
            where.append("created<?")
            args.append(before)
        if since is not None:
            where.append("created>=?")
            args.append(since)
        with self.connect() as c:
            if after:
                anchor = c.execute(f"SELECT {field} AS sort_value,created,id FROM packet_summaries WHERE id=?", (after,)).fetchone()
                if anchor is None:
                    raise ValueError("分页位置已过期，请刷新请求列表")
                comparison = ">" if direction == "asc" else "<"
                where.append(f"({field},created,id){comparison}(?,?,?)")
                args.extend([anchor["sort_value"], anchor["created"], anchor["id"]])
            sql = "SELECT meta FROM packet_summaries" + (" WHERE " + " AND ".join(where) if where else "")
            ordering = f"created {order},id {order}" if sort == "created" else f"{field} {order},created {order},id {order}"
            rows = c.execute(sql + f" ORDER BY {ordering} LIMIT ?", [*args, limit]).fetchall()
        project = trend_packet_metadata if trend else packet_metadata
        return [project(json.loads(r["meta"])) for r in rows]

    def filter_options(self):
        kinds, statuses, threads = set(), set(), {}
        with self.connect() as c:
            rows = c.execute("SELECT thread,kind,status_key FROM packet_summaries WHERE method!='GET' ORDER BY created DESC,id DESC")
            for row in rows:
                kinds.add(row["kind"])
                statuses.add(row["status_key"])
                threads.setdefault(row["thread"] or "", None)
        return {"kinds": sorted(kinds), "statuses": sorted(statuses), "threads": list(threads)}

    def stats(self):
        with self.connect() as c:
            row = c.execute("SELECT count(*) AS count,coalesce(sum(stored_bytes),0) AS stored,coalesce(sum(CASE WHEN capture_owner='existing_exit' AND state IN ('captured','streaming') THEN 1 ELSE 0 END),0) AS observed_active,(SELECT revision FROM packet_index_state WHERE singleton=1) AS revision FROM packet_summaries").fetchone()
            detail_count = c.execute(f"SELECT count(*) FROM packets WHERE {RETAINED_DETAILS}").fetchone()[0]
            threads = c.execute("SELECT thread,count(*) AS count FROM packet_summaries GROUP BY thread ORDER BY max(created) DESC LIMIT 120").fetchall()
        disk_bytes = 0
        for path in self.directory.glob("packets.sqlite*"):
            try:
                if path.is_file():
                    disk_bytes += path.stat().st_size
            except FileNotFoundError:
                pass  # A concurrent SQLite close can checkpoint and unlink WAL files.
        return {**dict(row), "disk_bytes": disk_bytes, "max_bytes": self.max_bytes, "max_count": self.max_count,
                "detail_count": detail_count,
                "max_detail_count": max(0, min(self.max_count, self.max_count if self.max_detail_count is None else self.max_detail_count)),
                "threads": [dict(r) for r in threads]}

    def compare(self, packet_id):
        current, _ = self.get(packet_id)
        if current.get("details_evicted"):
            return {"state": "details_unavailable"}
        if not current.get("thread_id"):
            return None  # Avoid inventing a same-chat comparison for unidentified requests.
        with self.connect() as c:
            row = c.execute("SELECT meta FROM packets WHERE thread=? AND created<? AND json_extract(meta,'$.source')=? ORDER BY created DESC LIMIT 1",
                            (current.get("thread_id", ""), current["created"], current["source"])).fetchone()
        if row is None:
            return None
        previous = json.loads(row["meta"])
        if previous.get("details_evicted"):
            return {"previous_id": previous["id"], "state": "details_unavailable"}
        return {"previous_id": previous["id"], **compare_packets(previous, current)}
