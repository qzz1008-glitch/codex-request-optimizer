"""Read-only session labels and recent packet/window metadata for the sidebar."""
from contextlib import closing
from collections import OrderedDict
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import threading
import zlib

MAX_SESSIONS = 300
INDEX_TASK_PREFIX = "You are in a fork of an existing Codex thread.\nFill the structured description field"


def index_task_title(body):
    inputs = body.get("input") if isinstance(body, dict) else None
    if not isinstance(inputs, list):
        return None
    for item in reversed(inputs):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        text = content if isinstance(content, str) else "\n".join(
            part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        ) if isinstance(content, list) else ""
        if not text.startswith(INDEX_TASK_PREFIX) or "This is a keyword retrieval index, not a broad prose summary." not in text:
            return None
        titles = [line[len("Current title: "):].strip() for line in text.splitlines() if line.startswith("Current title: ")]
        return titles[0][:512] if len(titles) == 1 and titles[0] else None
    return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def subagent_title(source, name=None, nickname=None):
    try:
        source = json.loads(source)
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    except (ValueError, TypeError):
        return None
    if not isinstance(spawn, dict):
        return None
    task = name if isinstance(name, str) and name.strip() else spawn.get("agent_path")
    task = task.strip().rstrip("/").rsplit("/", 1)[-1][:256] if isinstance(task, str) else ""
    agent = nickname if isinstance(nickname, str) and nickname.strip() else spawn.get("agent_nickname")
    agent = agent.strip()[:128] if isinstance(agent, str) else ""
    if not task and not agent:
        return None
    return "子任务 · " + (task + ("（" + agent + "）" if agent and agent != task else "") if task else agent)


class SessionSummaries:
    def __init__(self, token_windows):
        self.tokens = token_windows
        self._cursor = 0
        self._token_states = {}
        self._request_labels = OrderedDict()
        self._lock = threading.Lock()

    def _labels(self, ids):
        if not ids:
            return {}
        databases = sorted(self.tokens.home.glob("state_*.sqlite"),
                           key=lambda p: int(p.stem[6:]) if p.stem[6:].isdigit() else -1, reverse=True)
        for database in databases[:3]:
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                    db.execute("PRAGMA query_only=ON")
                    columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                    updated = "coalesce(nullif(updated_at_ms,0)/1000.0,updated_at)" if "updated_at_ms" in columns else "updated_at"
                    extras = ["substr(" + column + ",1," + str(limit) + ")" if column in columns else "NULL"
                              for column, limit in (("name", 512), ("agent_nickname", 128), ("source", 8192))]
                    rows = db.execute("SELECT id,substr(title,1,512)," + updated + "," + ",".join(extras) + " FROM threads WHERE id IN (" +
                                      ",".join("?" for _ in ids) + ")", ids).fetchall()
                # Codex App stores its display name separately from the initial-message title.
                return {r[0]: {"title": r[3] if isinstance(r[3], str) and r[3].strip() else r[1], "updated_at": r[2],
                               "fallback_title": subagent_title(r[5], r[3], r[4])} for r in rows}
            except (OSError, sqlite3.Error):
                continue
        return {}

    def _index_labels(self, ids):
        if not ids:
            return {}
        try:
            path = self.tokens.home / "session_index.jsonl"
            offset = max(0, path.stat().st_size - 1024 * 1024)
            with path.open("rb") as stream:
                stream.seek(offset)
                lines = stream.read(1024 * 1024).split(b"\n")
            wanted, found = set(ids), {}
            for line in reversed(lines[1:-1] if offset else lines[:-1]):
                try:
                    record = json.loads(line)
                    thread = record.get("id")
                    name = record.get("thread_name")
                    if thread not in wanted or thread in found or not isinstance(name, str) or not name:
                        continue
                    try:
                        updated = datetime.fromisoformat(record.get("updated_at", "").replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        updated = None
                    found[thread] = {"title": name[:512], "updated_at": updated}
                except (ValueError, TypeError, AttributeError):
                    continue
            return found
        except OSError:
            return {}

    def read(self, data_dir):
        path = (Path(data_dir) / "packets.sqlite").resolve()
        records = []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            groups = db.execute("SELECT coalesce(thread,''),count(*),max(created) FROM packet_summaries "
                                "WHERE method!='GET' GROUP BY coalesce(thread,'') "
                                "ORDER BY max(created) DESC,coalesce(thread,'') LIMIT ?", (MAX_SESSIONS,)).fetchall()
            for thread, count, created in groups:
                row = db.execute("SELECT id,created,meta FROM packet_summaries "
                                 "WHERE coalesce(thread,'')=? AND method!='GET' "
                                 "ORDER BY created DESC,id DESC LIMIT 1", (thread,)).fetchone()
                if not row:
                    continue
                try:
                    meta = json.loads(row[2])
                    size = meta.get("outbound_wire_bytes")
                except (ValueError, TypeError, AttributeError):
                    size = None
                records.append({"thread_id": thread, "packet_count": count, "updated_at": created,
                                "title": None, "latest_packet": {"id": row[0], "created": row[1],
                                "wire_bytes": size if number(size) and size >= 0 else None}})
        labels = self._labels([r["thread_id"] for r in records if r["thread_id"]])
        missing = [r["thread_id"] for r in records if r["thread_id"] and not labels.get(r["thread_id"], {}).get("title")]
        for thread, label in self._index_labels(missing).items():
            previous = labels.get(thread, {})
            if number(previous.get("updated_at")) and (not number(label["updated_at"]) or previous["updated_at"] > label["updated_at"]):
                label["updated_at"] = previous["updated_at"]
            labels[thread] = label
        with self._lock:
            unread_labels = 0
            ids = [r["thread_id"] for r in records if r["thread_id"]]
            self._token_states = {k: v for k, v in self._token_states.items() if k in ids}
            # At most eight bounded history reads per refresh, rotating through larger lists.
            for offset in range(min(8, len(ids))):
                index = (self._cursor + offset) % len(ids)
                self._token_states[ids[index]] = self.tokens.latest(ids[index])
            self._cursor = (self._cursor + min(8, len(ids))) % len(ids) if ids else 0
            for record in records:
                label = labels.get(record["thread_id"], {})
                record["title"] = label.get("title")
                if not record["title"] and label.get("fallback_title"):
                    record["title"] = label["fallback_title"]
                if not record["title"] and record["thread_id"]:
                    packet_id = record["latest_packet"]["id"]
                    if packet_id not in self._request_labels and unread_labels < 8:
                        self._request_labels[packet_id] = self._request_title(path, packet_id, record["thread_id"])
                        unread_labels += 1
                        while len(self._request_labels) > MAX_SESSIONS:
                            self._request_labels.popitem(last=False)
                    source_title = self._request_labels.get(packet_id)
                    if source_title:
                        record.update(title="会话索引 · " + source_title, title_source="index_task", source_title=source_title)
                updated = label.get("updated_at")
                if number(updated):
                    record["updated_at"] = max(record["updated_at"], updated)
                record["token_window"] = self._token_states.get(record["thread_id"], {"state": "loading" if record["thread_id"] else "unavailable"})
        records.sort(key=lambda r: (-r["updated_at"], r["thread_id"]))
        return {"state": "available", "sessions": records}

    def _request_title(self, database, packet_id, thread):
        """Identify native ephemeral indexing requests from their saved, redacted copy."""
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.execute("PRAGMA query_only=ON")
                row = db.execute("SELECT substr(meta,1,1048577),CASE WHEN length(content)<=2097152 THEN content END "
                                 "FROM packets WHERE id=? AND thread=?", (packet_id, thread)).fetchone()
            if not row or len(row[0]) > 1048576:
                return None
            meta = json.loads(row[0])
            items = meta.get("input_items") or []
            if not any(isinstance(item, dict) and item.get("kind") == "message:user"
                       and isinstance(item.get("label"), str) and item["label"].startswith(INDEX_TASK_PREFIX) for item in items):
                return None
            if not row[1]:
                return None
            inflater = zlib.decompressobj()
            raw = inflater.decompress(row[1], 8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024 or not inflater.eof:
                return None
            content = json.loads(raw)
            return index_task_title(content.get("body")) if isinstance(content, dict) else None
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, zlib.error):
            return None
