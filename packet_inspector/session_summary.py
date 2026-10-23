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
                              temporary_request_kind, request_session_context, session_id)

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
                    try:
                        edges = db.execute("SELECT child_thread_id,min(parent_thread_id) FROM thread_spawn_edges WHERE child_thread_id IN (" +
                                           ",".join("?" for _ in ids) + ") GROUP BY child_thread_id HAVING count(DISTINCT parent_thread_id)=1", ids).fetchall()
                    except sqlite3.OperationalError:
                        edges = []
                # Codex App stores its display name separately from the initial-message title.
                labels = {r[0]: {"title": r[3] if isinstance(r[3], str) and r[3].strip() else r[1], "updated_at": r[2],
                                "fallback_title": subagent_title(r[5], r[3], r[4]),
                                **({"session_kind": "subagent"} if subagent_source(r[5]) else {})} for r in rows}
                parents = {}
                for child, parent in edges:
                    if session_id(parent) and child != parent:
                        parents.setdefault(child, set()).add(parent)
                for row in rows:
                    spawn = (subagent_source(row[5]) or {}).get("thread_spawn", {})
                    parent = session_id(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None
                    if parent and parent != row[0]:
                        parents.setdefault(row[0], set()).add(parent)
                for child, candidates in parents.items():
                    if len(candidates) == 1:
                        labels.setdefault(child, {}).update(parent_thread_id=candidates.pop(), session_kind="subagent")
                return labels
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

    def _prepare_records(self, database, records, labels):
        unread = 0
        for record in records:
            label = labels.get(record["thread_id"], {})
            record["title"] = label.get("title")
            if not record["title"] and label.get("fallback_title"):
                record.update(title=label["fallback_title"], title_source="subagent")
            if label.get("session_kind"):
                record["session_kind"] = label["session_kind"]
            if not record["title"] and record["thread_id"] and record.get("latest_packet"):
                packet_id = record["latest_packet"]["id"]
                if packet_id not in self._request_labels and unread < 8:
                    self._request_labels[packet_id] = self._request_metadata(database, packet_id, record["thread_id"])
                    unread += 1
                    while len(self._request_labels) > MAX_SESSIONS:
                        self._request_labels.popitem(last=False)
                record.update(self._request_labels.get(packet_id) or {})
            parent = label.get("parent_thread_id")
            if parent:
                if record.get("parent_thread_id") not in (None, parent):
                    record.pop("parent_thread_id", None)
                else:
                    record["parent_thread_id"] = parent
            if number(label.get("updated_at")):
                record["updated_at"] = max(record["updated_at"], label["updated_at"])
        # A parent can have native history but no captured request of its own.
        known = {record["thread_id"] for record in records}
        for _ in range(8):
            parents = sorted({record["parent_thread_id"] for record in records
                              if record.get("parent_thread_id") and record["parent_thread_id"] not in known})
            if not parents or len(known) + len(parents) > MAX_SESSIONS * 2:
                break
            parent_labels = self._labels(parents)
            for thread, indexed in self._index_labels([p for p in parents if not parent_labels.get(p, {}).get("title")]).items():
                parent_labels.setdefault(thread, {}).update(indexed)
            for thread in parents:
                label = parent_labels.get(thread, {})
                title = label.get("title") or label.get("fallback_title")
                parent = {"thread_id": thread, "packet_count": 0, "updated_at": label["updated_at"] if number(label.get("updated_at")) else 0,
                          "title": title, "latest_packet": None}
                if label.get("session_kind"):
                    parent["session_kind"] = label["session_kind"]
                if label.get("parent_thread_id"):
                    parent["parent_thread_id"] = label["parent_thread_id"]
                if not label.get("title") and label.get("fallback_title"):
                    parent["title_source"] = "subagent"
                records.append(parent)
                known.add(thread)

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
                    meta, size = {}, None
                records.append({"thread_id": thread, "packet_count": count, "updated_at": created,
                                "title": None, "latest_packet": {"id": row[0], "created": row[1],
                                "wire_bytes": size if number(size) and size >= 0 else None},
                                **({"parent_thread_id": session_id(meta.get("parent_thread_id"))}
                                   if session_id(meta.get("parent_thread_id")) else {}),
                                **({"session_kind": meta["session_kind"]}
                                   if isinstance(meta.get("session_kind"), str) and meta["session_kind"] in
                                      {"image_analysis", "next_message_suggestion", "title_generation", "session_index", "subagent"} else {})})
        labels = self._labels([r["thread_id"] for r in records if r["thread_id"]])
        missing = [r["thread_id"] for r in records if r["thread_id"] and not labels.get(r["thread_id"], {}).get("title")]
        for thread, label in self._index_labels(missing).items():
            previous = labels.get(thread, {})
            if number(previous.get("updated_at")) and (not number(label["updated_at"]) or previous["updated_at"] > label["updated_at"]):
                label["updated_at"] = previous["updated_at"]
            labels[thread] = {**previous, **label}
        with self._lock:
            self._prepare_records(path, records, labels)
            ids = [r["thread_id"] for r in records if r["thread_id"]]
            self._token_states = {k: v for k, v in self._token_states.items() if k in ids}
            # At most eight bounded history reads per refresh, rotating through larger lists.
            for offset in range(min(8, len(ids))):
                index = (self._cursor + offset) % len(ids)
                self._token_states[ids[index]] = self.tokens.latest(ids[index])
            self._cursor = (self._cursor + min(8, len(ids))) % len(ids) if ids else 0
            for record in records:
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
                context = {"session_kind": kind} if kind else {}
                parent = session_id(meta.get("parent_thread_id"))
                if parent:
                    context["parent_thread_id"] = parent
                items = meta.get("input_items") or []
                has_metadata = any(isinstance(field, dict) and field.get("name") in {"client_metadata", "metadata"}
                                   for field in meta.get("top_fields", []))
                if not has_metadata and not any(isinstance(item, dict) and item.get("kind") == "message:user"
                           and isinstance(item.get("label"), str) and item["label"].startswith(
                               (INDEX_TASK_PREFIX, "<image name=", "You are a one-shot image analyst.",
                                "You are an isolated one-shot image analyst.")) for item in items):
                    return context or None
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
            context.update(request_session_context(body))
            source_title = index_task_title(body)
            if source_title:
                return {**context, "title": "会话索引 · " + source_title, "title_source": "index_task", "source_title": source_title,
                        "session_kind": "session_index"}
            kind = temporary_request_kind(body)
            if kind:
                context["session_kind"] = kind
            return context or None
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, zlib.error):
            return None
