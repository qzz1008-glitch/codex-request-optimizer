"""Read-only session labels and recent packet/token metadata for the sidebar."""
from contextlib import closing
from collections import OrderedDict
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import threading
import zlib

from .request_context import (AUXILIARY_KINDS, IMAGE_ANALYST_PREFIXES, last_user_text, temporary_kind_from_metadata,
                              temporary_request_kind, request_session_context, session_id)
from .response_usage import packet_usage
from .native_state import WSL_HOST_ID, read_state, state_databases, thread_origins
from .token_estimate import estimate_usage, overflow_applied
from .token_window import is_token_request
from .usage_trigger import REPORTED_INPUT_TOKENS

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


def external_labels(raw, ids, labels=None):
    """Normalize the read-only native index results from a Linux host."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid native session index response")
    labels, parents = dict(labels or {}), {}
    for child, parent in raw.get("edges", []):
        if child in ids and session_id(parent) and child != parent:
            parents.setdefault(child, set()).add(parent)
    for thread, title, name, nickname, source, updated in raw.get("rows", []):
        if thread not in ids:
            continue
        subagent = subagent_source(source)
        spawn = subagent.get("thread_spawn", {}) if subagent else {}
        parent = session_id(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None
        if parent and parent != thread:
            parents.setdefault(thread, set()).add(parent)
        labels[thread] = {"title": name if isinstance(name, str) and name.strip() else title or labels.get(thread, {}).get("title"),
                          "updated_at": updated, "fallback_title": subagent_title(source, name, nickname),
                          **({"session_kind": "subagent"} if subagent else {})}
    for thread, title in raw.get("index", {}).items():
        if thread in ids and isinstance(title, str) and title.strip() and not labels.get(thread, {}).get("title"):
            labels.setdefault(thread, {})["title"] = title[:512]
    for thread, values in parents.items():
        if len(values) == 1:
            labels.setdefault(thread, {}).update(parent_thread_id=next(iter(values)), session_kind="subagent")
    return labels


def connection_scope(metadata):
    for kind in ("ssh", "wsl"):
        key = metadata.get(kind + "_connection_key")
        if isinstance(key, str) and key:
            return kind, key
    return None


def _packet_summary(thread, row):
    packet_id, created, method, raw = row
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        meta = None
    packet = meta if isinstance(meta, dict) else {}
    packet.update(id=packet_id, created=created, method=method, thread_id=thread)
    return packet


def _earlier_model_packets(db, latest):
    thread = latest["thread_id"]
    if thread:
        rows = db.execute("""SELECT id,created,method,meta FROM packet_summaries
                            WHERE thread=? AND method='POST' AND (created,id)<(?,?)
                            ORDER BY created DESC,id DESC""", (thread, latest["created"], latest["id"]))
        for row in rows:
            packet = _packet_summary(thread, row)
            if is_token_request(packet, include_compaction=True):
                yield packet


def _latest_model_packet(db, latest):
    if is_token_request(latest, include_compaction=True):
        return latest
    return next(_earlier_model_packets(db, latest), None)


def _captured_token_window(packet):
    if overflow_applied(packet):
        estimate = estimate_usage(packet)
        return {**estimate, "recorded_at": packet["created"]} if estimate else {"state": "unavailable"}
    usage = packet_usage(packet)
    if usage is not None:
        return {"state": "available", "source": "upstream_response",
                "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                "used_tokens": usage["total_tokens"], "recorded_at": packet["created"],
                "usage_override_applied": bool(packet.get("usage_override_applied"))}
    if (not is_token_request(packet) or packet.get("usage_override_applied")
            or packet.get("usage_override_request_bytes") is not None):
        return {"state": "unavailable"}
    return None


def _has_input_tokens(window):
    return (window is not None and window["state"] == "available"
            and (not window.get("estimated") or window.get("source") == "codex_estimate"))


def _previous_token_window(db, latest, last_native):
    next_created = latest["created"]
    for packet in _earlier_model_packets(db, latest):
        window = _captured_token_window(packet)
        if _has_input_tokens(window):
            return window
        if window is None and last_native:
            # Keep the original native attribution within the next model request's boundary.
            order, native = last_native
            if order == (packet["created"], packet["id"]) and native["recorded_at"] < next_created:
                return native
        next_created = packet["created"]
    return None


class SessionSummaries:
    def __init__(self, token_windows, *, wsl_labels=None, ssh_labels=None, ssh_origins=None):
        self.tokens = token_windows
        self.connection_labels = {kind: reader for kind, reader in (("wsl", wsl_labels), ("ssh", ssh_labels)) if reader}
        self.ssh_origins = ssh_origins
        self._cursor = 0
        self._token_states = {}
        self._last_native_tokens = {}
        self._request_labels = OrderedDict()
        self._lock = threading.Lock()

    def _labels(self, ids, captured_origins=None, native_origins=None):
        if not ids:
            return {}
        captured_origins = captured_origins or {}
        native_origins = native_origins or {}
        local_ids = [thread for thread in ids if thread not in captured_origins
                     and native_origins.get(thread, (None, None))[0] != WSL_HOST_ID]
        found, resolved, parent_candidates = {}, set(), {}
        for database in state_databases(self.tokens.home):
            remaining = [thread for thread in local_ids if thread not in resolved]
            if not remaining:
                break
            try:
                with read_state(database) as db:
                    columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                    if "id" not in columns:
                        continue
                    timestamps = (["nullif(updated_at_ms,0)/1000.0"] if "updated_at_ms" in columns else [])
                    timestamps += ["updated_at"] if "updated_at" in columns else []
                    updated = "coalesce(" + ",".join(timestamps + ["NULL"]) + ")" if timestamps else "NULL"
                    title = "substr(title,1,512)" if "title" in columns else "NULL"
                    extras = ["substr(" + column + ",1," + str(limit) + ")" if column in columns else "NULL"
                              for column, limit in (("name", 512), ("agent_nickname", 128), ("source", 8192))]
                    rows = db.execute("SELECT id," + title + "," + updated + "," + ",".join(extras) + " FROM threads WHERE id IN (" +
                                      ",".join("?" for _ in remaining) + ")", remaining).fetchall()
                    try:
                        edges = db.execute("SELECT child_thread_id,min(parent_thread_id) FROM thread_spawn_edges WHERE child_thread_id IN (" +
                                           ",".join("?" for _ in remaining) + ") GROUP BY child_thread_id HAVING count(DISTINCT parent_thread_id)=1", remaining).fetchall()
                    except sqlite3.OperationalError:
                        edges = []
                # Codex App stores its display name separately from the initial-message title.
                labels = {r[0]: {"title": r[3] if isinstance(r[3], str) and r[3].strip() else r[1], "updated_at": r[2],
                                "origin": {"kind": "local"},
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
                    known = parent_candidates.setdefault(child, set())
                    known.update(candidates)
                    if len(known) == 1:
                        labels.setdefault(child, {}).update(parent_thread_id=next(iter(known)), session_kind="subagent")
                    else:
                        # A new index can contain an edge before its thread row
                        # migrates. Conflicting older evidence is not a parent.
                        found.get(child, {}).pop("parent_thread_id", None)
                for thread, label in labels.items():
                    # A newer row is authoritative, even if its title was
                    # deliberately cleared. Only absent threads use old indexes.
                    target = found.setdefault(thread, {})
                    for key, value in label.items():
                        target.setdefault(key, value)
                resolved.update(row[0] for row in rows)
            except (OSError, sqlite3.Error):
                continue
        missing = [thread for thread in local_ids if not found.get(thread, {}).get("title")]
        for thread, label in self._index_labels(missing).items():
            previous = found.get(thread, {})
            if number(previous.get("updated_at")) and (not number(label["updated_at"]) or previous["updated_at"] > label["updated_at"]):
                label["updated_at"] = previous["updated_at"]
            found[thread] = {**previous, **label}
        self._lookup_connected_labels(ids, captured_origins, native_origins, found, resolved)
        return found

    def _lookup_connected_labels(self, ids, captured_origins, native_origins, found, resolved):
        requests = {kind: {} for kind in self.connection_labels}
        for thread in ids:
            if thread in captured_origins:
                kind, key = captured_origins[thread]
                if kind in requests:
                    requests[kind][thread] = (key, None)
                continue
            host_id, display = native_origins.get(thread, (None, None))
            if host_id == WSL_HOST_ID:
                if "wsl" in requests:
                    requests["wsl"][thread] = (None, host_id)
            elif host_id is None and thread not in found and thread not in resolved:
                for pending in requests.values():
                    pending[thread] = (None, None)
            elif host_id not in (None, "local") and "ssh" in requests:
                known_remote = display and display.get("kind") == "remote"
                if known_remote or (thread not in found and thread not in resolved):
                    requests["ssh"][thread] = (None, host_id)

        results = {kind: self.connection_labels[kind](pending) for kind, pending in requests.items() if pending}
        for thread in ids:
            candidates = [results[kind] for kind in results if thread in requests[kind]]
            if candidates and all(thread in result for result in candidates):
                matches = [label for result in candidates for label in result[thread]]
                if len(matches) == 1:
                    found[thread] = matches[0]

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

    def _prepare_records(self, database, records, labels, captured_origins, native_origins):
        unread = 0
        for record in records:
            label = labels.get(record["thread_id"], {})
            scope = connection_scope(label)
            if scope:
                captured_origins[record["thread_id"]] = scope
            record["title"] = label.get("title")
            record["origin"] = label.get("origin")
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
            unseen_parents = set(parents)
            parent_origins = {}
            for record in records:
                parent = record.get("parent_thread_id")
                origin = captured_origins.get(record["thread_id"])
                if parent in unseen_parents and origin:
                    parent_origins.setdefault(parent, set()).add(origin)
            captured_origins.update((parent, next(iter(origins))) for parent, origins in parent_origins.items() if len(origins) == 1)
            parent_labels = self._labels(parents, captured_origins, native_origins)
            for thread in parents:
                label = parent_labels.get(thread, {})
                scope = connection_scope(label)
                if scope:
                    captured_origins[thread] = scope
                title = label.get("title") or label.get("fallback_title")
                parent = {"thread_id": thread, "packet_count": 0, "updated_at": label["updated_at"] if number(label.get("updated_at")) else 0,
                          "title": title, "latest_packet": None, "origin": label.get("origin")}
                if label.get("session_kind"):
                    parent["session_kind"] = label["session_kind"]
                if label.get("parent_thread_id"):
                    parent["parent_thread_id"] = label["parent_thread_id"]
                if not label.get("title") and label.get("fallback_title"):
                    parent["title_source"] = "subagent"
                records.append(parent)
                known.add(thread)

    def _native_token_window(self, path, packet):
        thread = packet["thread_id"]
        # Old summaries may have no endpoint or response timing to match against.
        if any(field in packet for field in ("path", "status", "state", "model", "duration_ms")):
            window = self.tokens.lookup(packet)
        else:
            window = self.tokens.latest(thread)
        if window["state"] != "available" or window.get("estimated"):
            return window
        # A late native notification may echo an earlier relay override, even
        # after a new request starts. Read the relay's provenance after the log.
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                override = db.execute("""SELECT 1 FROM packet_summaries
                    WHERE thread=? AND created<=? AND method='POST'
                    AND CASE WHEN json_valid(meta) THEN json_type(meta,'$.usage_override_applied')='true'
                        AND CASE WHEN json_type(meta,'$.reported_usage.input_tokens')='integer'
                                 THEN json_extract(meta,'$.reported_usage.input_tokens') ELSE ? END=?
                        ELSE 0 END LIMIT 1""",
                    (thread, window["recorded_at"], REPORTED_INPUT_TOKENS, window["input_tokens"])).fetchone()
        except sqlite3.Error:
            return {"state": "unavailable"}
        return {"state": "unavailable"} if override else window

    def read(self, data_dir):
        path = (Path(data_dir) / "packets.sqlite").resolve()
        records = []
        model_packets = {}
        captured_origins = {}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            groups = db.execute("SELECT coalesce(thread,''),count(*),max(created) FROM packet_summaries "
                                "WHERE method!='GET' GROUP BY coalesce(thread,'') "
                                "ORDER BY max(created) DESC,coalesce(thread,'') LIMIT ?", (MAX_SESSIONS,)).fetchall()
            for thread, count, created in groups:
                # Keep known IDs searchable by the existing thread/time index.
                # Older stores may use either NULL or '' for an unknown ID.
                condition = "thread=?" if thread else "coalesce(thread,'')=?"
                row = db.execute("SELECT id,created,method,meta FROM packet_summaries "
                                 "WHERE " + condition + " AND method!='GET' "
                                 "ORDER BY created DESC,id DESC LIMIT 1", (thread,)).fetchone()
                if not row:
                    continue
                meta = _packet_summary(thread, row)
                size = meta.get("outbound_wire_bytes")
                scope = connection_scope(meta)
                if thread and meta.get("source") == "live" and scope:
                    captured_origins[thread] = scope
                model_packets[thread] = _latest_model_packet(db, meta)
                records.append({"thread_id": thread, "packet_count": count, "updated_at": created,
                                "title": None, "latest_packet": {"id": row[0], "created": row[1],
                                "wire_bytes": size if number(size) and size >= 0 else None},
                                **({"parent_thread_id": session_id(meta.get("parent_thread_id"))}
                                   if session_id(meta.get("parent_thread_id")) else {}),
                                **({"session_kind": meta["session_kind"]}
                                   if isinstance(meta.get("session_kind"), str) and meta["session_kind"] in
                                      AUXILIARY_KINDS else {})})
        native_origins = thread_origins(self.tokens.home)
        labels = self._labels([r["thread_id"] for r in records if r["thread_id"]], captured_origins, native_origins)
        with self._lock:
            self._prepare_records(path, records, labels, captured_origins, native_origins)
            fallback = []
            for record in records:
                thread = record["thread_id"]
                packet = model_packets.get(thread)
                window = _captured_token_window(packet) if packet else None
                if window is None and thread and packet:
                    fallback.append((record, packet))
                else:
                    record["token_window"] = window or {"state": "unavailable"}
            ids = {packet["id"] for _, packet in fallback}
            self._token_states = {k: v for k, v in self._token_states.items() if k in ids}
            self._last_native_tokens = {thread: value for thread, value in self._last_native_tokens.items()
                                        if model_packets.get(thread)}
            # At most eight bounded history reads per refresh, rotating through larger lists.
            for offset in range(min(8, len(fallback))):
                _, packet = fallback[(self._cursor + offset) % len(fallback)]
                window = self._native_token_window(path, packet)
                self._token_states[packet["id"]] = window
                thread = packet["thread_id"]
                order = (packet["created"], packet["id"])
                last_native = self._last_native_tokens.get(thread)
                if _has_input_tokens(window):
                    if last_native is None or order >= last_native[0]:
                        self._last_native_tokens[thread] = (order, window)
                elif last_native and order == last_native[0]:
                    del self._last_native_tokens[thread]
            self._cursor = (self._cursor + min(8, len(fallback))) % len(fallback) if fallback else 0
            for record, packet in fallback:
                record["token_window"] = self._token_states.get(packet["id"], {"state": "loading"})
            missing = [record for record in records if record["thread_id"] and model_packets.get(record["thread_id"])
                       and not _has_input_tokens(record["token_window"])]
            if missing:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                    for record in missing:
                        thread = record["thread_id"]
                        previous = _previous_token_window(db, model_packets[thread], self._last_native_tokens.get(thread))
                        if previous is not None:
                            record["token_window"] = previous
        ssh_keys = {key for kind, key in captured_origins.values() if kind == "ssh"}
        ssh_origins = self.ssh_origins(ssh_keys) if ssh_keys and self.ssh_origins else {}
        for record in records:
            thread = record["thread_id"]
            kind, key = captured_origins.get(thread, (None, None))
            host_id, origin = native_origins.get(thread, (None, record["origin"]))
            if kind == "ssh" and key in ssh_origins:
                route_host_id, route_origin = ssh_origins[key]
                if host_id != route_host_id or not origin:
                    origin = route_origin
            record["origin"] = origin
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
