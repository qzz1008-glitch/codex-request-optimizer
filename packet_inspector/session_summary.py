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

from .request_context import (IMAGE_ANALYST_PREFIXES, last_user_text, temporary_kind_from_metadata,
                              temporary_request_kind)

MAX_SESSIONS = 300
INDEX_TASK_PREFIX = "You are in a fork of an existing Codex thread.\nFill the structured description field"


def index_task_title(body):
    text = last_user_text(body)
    if not text or not text.startswith(INDEX_TASK_PREFIX) or "This is a keyword retrieval index, not a broad prose summary." not in text:
        return None
    titles = [line[len("Current title: "):].strip() for line in text.splitlines() if line.startswith("Current title: ")]
    return titles[0][:512] if len(titles) == 1 and titles[0] else None


def image_analysis_request(body):
    return temporary_request_kind(body) == "image_analysis"


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def subagent_source(source):
    try:
        source = json.loads(source)
        subagent = source.get("subagent") if isinstance(source, dict) else None
    except (ValueError, TypeError):
        return None
    return subagent if isinstance(subagent, dict) and subagent else None


def subagent_title(source, name=None, nickname=None):
    subagent = subagent_source(source)
    spawn = subagent.get("thread_spawn") if subagent else None
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
                               "fallback_title": subagent_title(r[5], r[3], r[4]),
                               **({"session_kind": "subagent"} if subagent_source(r[5]) else {})} for r in rows}
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
        reported_packets = {}
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
                    if meta.get("usage_override_applied") or meta.get("upstream_usage"):
                        reported_packets[thread] = meta
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
            labels[thread] = {**previous, **label}
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
                    record["title_source"] = "subagent"
                if label.get("session_kind"):
                    record["session_kind"] = label["session_kind"]
                if not record["title"] and record["thread_id"]:
                    packet_id = record["latest_packet"]["id"]
                    if packet_id not in self._request_labels and unread_labels < 8:
                        self._request_labels[packet_id] = self._request_metadata(path, packet_id, record["thread_id"])
                        unread_labels += 1
                        while len(self._request_labels) > MAX_SESSIONS:
                            self._request_labels.popitem(last=False)
                    request_metadata = self._request_labels.get(packet_id)
                    if request_metadata:
                        record.update(request_metadata)
                updated = label.get("updated_at")
                if number(updated):
                    record["updated_at"] = max(record["updated_at"], updated)
                record["token_window"] = self._token_states.get(record["thread_id"], {"state": "loading" if record["thread_id"] else "unavailable"})
                packet = reported_packets.get(record["thread_id"], {})
                usage = packet.get("upstream_usage")
                if (isinstance(usage, dict) and packet.get("status") == 200
                        and packet.get("state") in {"streaming", "complete"}
                        and all(type(usage.get(k)) is int and usage[k] >= 0
                                for k in ("input_tokens", "output_tokens", "total_tokens"))):
                    window = record["token_window"].get("window_tokens")
                    record["token_window"] = {"state": "available", "source": "upstream_response",
                        "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                        "used_tokens": usage["total_tokens"], "window_tokens": window,
                        "recorded_at": record["latest_packet"]["created"],
                        "usage_override_applied": bool(packet.get("usage_override_applied"))}
                elif packet.get("usage_override_applied"):
                    record["token_window"] = {"state": "unavailable"}
        records.sort(key=lambda r: (-r["updated_at"], r["thread_id"]))
        return {"state": "available", "sessions": records}

    def _request_metadata(self, database, packet_id, thread):
        """Identify temporary tasks from their saved, redacted request text."""
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.execute("PRAGMA query_only=ON")
                row = db.execute("SELECT substr(meta,1,1048577) FROM packets WHERE id=? AND thread=?",
                                 (packet_id, thread)).fetchone()
                if not row or len(row[0]) > 1048576:
                    return None
                meta = json.loads(row[0])
                kind = temporary_kind_from_metadata(meta)
                if kind:
                    return {"session_kind": kind}
                items = meta.get("input_items") or []
                if not any(isinstance(item, dict) and item.get("kind") == "message:user"
                           and isinstance(item.get("label"), str) and item["label"].startswith(
                               (INDEX_TASK_PREFIX, "<image name=", "You are a one-shot image analyst.",
                                "You are an isolated one-shot image analyst.")) for item in items):
                    return None
                content_row = db.execute("SELECT CASE WHEN length(content)<=2097152 THEN content END "
                                         "FROM packets WHERE id=? AND thread=?", (packet_id, thread)).fetchone()
            if not content_row or not content_row[0]:
                return None
            inflater = zlib.decompressobj()
            raw = inflater.decompress(content_row[0], 8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024 or not inflater.eof:
                return None
            content = json.loads(raw)
            body = content.get("body") if isinstance(content, dict) else None
            source_title = index_task_title(body)
            if source_title:
                return {"title": "会话索引 · " + source_title, "title_source": "index_task", "source_title": source_title,
                        "session_kind": "session_index"}
            kind = temporary_request_kind(body)
            if kind:
                return {"session_kind": kind}
            return None
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, zlib.error):
            return None
