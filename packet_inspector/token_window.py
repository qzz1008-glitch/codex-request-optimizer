"""Read per-response token counts from local Codex history without modifying it."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

READ_BUDGET = 8 * 1024 * 1024
LINE_LIMIT = 2 * 1024 * 1024
MAX_EVENTS = 20000


def is_token_request(packet, *, include_compaction=False):
    """Auxiliary HTTP calls cannot own a model's native usage notification."""
    if (packet.get("method", "POST") != "POST" or packet.get("request_kind") == "tool_search"
            or not include_compaction and packet.get("is_compaction")):
        return False
    path = packet.get("path")
    if not path:
        return True  # Older metadata may omit the endpoint.
    if not isinstance(path, str):
        return False
    try:
        endpoint = urlsplit(path).path.rstrip("/")
    except ValueError:
        return False
    return endpoint.endswith("/responses") or include_compaction and endpoint.endswith("/responses/compact")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _comparable(path):
    value = str(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


class TokenWindows:
    def __init__(self, codex_home=None):
        self.home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        self._files = OrderedDict()
        self._latest_files = OrderedDict()
        self._lock = threading.Lock()

    def _rollout(self, thread):
        if not isinstance(thread, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", thread):
            return None
        databases = sorted(self.home.glob("state_*.sqlite"),
                           key=lambda p: int(p.stem[6:]) if p.stem[6:].isdigit() else -1, reverse=True)
        for database in databases[:3]:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.execute("PRAGMA query_only=ON")
                row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread,)).fetchone()
            if not row:
                continue
            path = Path(row[0]).resolve()
            allowed = any(_comparable(path).is_relative_to(_comparable(self.home / folder))
                          for folder in ("sessions", "archived_sessions"))
            if allowed and path.name.startswith("rollout-") and path.suffix == ".jsonl" and path.is_file():
                return path
        return None

    def latest(self, thread):
        """Read the latest native window report with bounded tail reads and memory."""
        unavailable = {"state": "unavailable"}
        try:
            with self._lock:
                path = self._rollout(thread)
                if path is None:
                    return unavailable
                stat = path.stat()
                signature = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
                cached = self._latest_files.get(thread)
                if cached and cached[0] == signature:
                    self._latest_files.move_to_end(thread)
                    return dict(cached[1])
                result = unavailable
                with path.open("rb") as stream:
                    first = stream.readline(LINE_LIMIT)
                    header = json.loads(first) if first.endswith(b"\n") else {}
                    if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != thread:
                        return unavailable
                    offset = max(0, stat.st_size - 1024 * 1024)
                    stream.seek(offset)
                    lines = stream.read(1024 * 1024).split(b"\n")
                # Discard partial lines at either end of this snapshot.
                for line in reversed(lines[1:-1] if offset else lines[:-1]):
                    if b'"token_count"' not in line:
                        continue
                    try:
                        item = json.loads(line)
                        payload = item.get("payload") or {}
                        if item.get("type") != "event_msg" or payload.get("type") != "token_count":
                            continue
                        info = payload.get("info") or {}
                        usage = info.get("last_token_usage") or {}
                        values = [usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")]
                        if not all(type(n) is int and n >= 0 for n in values):
                            continue
                        inputs, outputs, total = values
                        if total != inputs + outputs and (inputs or outputs):
                            continue
                        window = info.get("model_context_window")
                        result = {"state": "available", "source": "codex_history", "used_tokens": total,
                                  "input_tokens": inputs, "output_tokens": outputs,
                                  "window_tokens": window if type(window) is int and window > 0 else None,
                                  "estimated": total != inputs + outputs,
                                  "recorded_at": datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()}
                        break
                    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                        continue
                self._latest_files[thread] = (signature, result)
                self._latest_files.move_to_end(thread)
                while len(self._latest_files) > 300:
                    self._latest_files.popitem(last=False)
                return dict(result)
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            return unavailable

    def _scan(self, path, thread):
        stat = path.stat()
        key = str(path)
        entry = self._files.get(key)
        identity = (stat.st_dev, stat.st_ino)
        if (entry is None or entry["identity"] != identity or stat.st_size < entry["offset"]
                or (stat.st_size == entry["size"] and stat.st_mtime_ns != entry["mtime"])):
            entry = {"identity": identity, "offset": 0, "size": 0, "mtime": 0, "skipping": False,
                     "session": None, "model": None, "events": [], "last_key": None}
            self._files[key] = entry
        self._files.move_to_end(key)
        while len(self._files) > 16:
            self._files.popitem(last=False)
        remaining = READ_BUDGET
        with path.open("rb") as stream:
            stream.seek(entry["offset"])
            while remaining > 0:
                line_start = stream.tell()
                line = stream.readline(LINE_LIMIT)
                if not line:
                    break
                remaining -= len(line)
                if not line.endswith(b"\n"):
                    if len(line) < LINE_LIMIT and not entry["skipping"]:
                        # An appending writer has not finished this line yet.
                        break
                    entry["skipping"] = True
                    entry["offset"] = stream.tell()
                    continue
                entry["offset"] = stream.tell()
                if entry["skipping"]:
                    entry["skipping"] = False
                    continue
                if not any(marker in line for marker in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                    continue
                try:
                    item = json.loads(line)
                    payload = item.get("payload") or {}
                    if item.get("type") == "session_meta":
                        # Forked rollouts retain parent headers after their own.
                        # Only the first record identifies this file's owner.
                        if line_start == 0:
                            entry["session"] = payload.get("id")
                    elif item.get("type") == "turn_context":
                        entry["model"] = payload.get("model")
                    elif item.get("type") == "event_msg" and payload.get("type") == "token_count":
                        self._event(entry, item, payload)
                except (ValueError, TypeError, AttributeError, OverflowError, KeyError):
                    continue
        entry["size"], entry["mtime"] = stat.st_size, stat.st_mtime_ns
        if entry["session"] != thread:
            return [], False
        return entry["events"], entry["offset"] < stat.st_size

    @staticmethod
    def _event(entry, item, payload):
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        values = [usage.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")]
        if not all(type(n) is int and n >= 0 for n in values):
            return
        inputs, outputs, total = values
        # Compaction/reset events can contain a local estimate in total_tokens only.
        if total != inputs + outputs or total == 0:
            return
        cumulative = info.get("total_token_usage") or {}
        event_key = (cumulative.get("total_tokens"), inputs, outputs, total, info.get("model_context_window"))
        if event_key == entry["last_key"] and cumulative.get("total_tokens") is not None:
            return
        timestamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()
        entry["last_key"] = event_key
        window = info.get("model_context_window")
        entry["events"].append({"at": timestamp, "model": entry["model"], "input_tokens": inputs,
                                "output_tokens": outputs, "used_tokens": total,
                                "window_tokens": window if type(window) is int and window > 0 else None})
        del entry["events"][:-MAX_EVENTS]

    def lookup(self, packet, next_created=None):
        unavailable = {"state": "unavailable"}
        if (not is_token_request(packet) or packet.get("method") != "POST"
                or packet.get("status") != 200 or packet.get("state") not in {"complete", "streaming"}):
            return unavailable
        created = packet.get("created")
        if not _number(created):
            return unavailable
        duration = packet.get("duration_ms")
        completed = created + duration / 1000 if _number(duration) and duration >= 0 else None
        if next_created is not None and (not _number(next_created) or next_created <= created
                                        or (completed is not None and next_created < completed)):
            return unavailable
        # Response usage may be persisted shortly after the relay closes its stream.
        end = min(completed + 5 if completed is not None else time.time(), next_created or float("inf"))
        try:
            with self._lock:
                path = self._rollout(packet.get("thread_id"))
                if path is None:
                    return unavailable
                events, loading = self._scan(path, packet["thread_id"])
                if loading:
                    return {"state": "loading"}
                candidates = [e for e in events if created <= e["at"] < end
                              and (completed is None or e["at"] >= completed - 2)
                              and e["model"] == packet.get("model")]
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return unavailable
        if len(candidates) != 1:
            waiting = next_created is None and (completed is None or time.time() < completed + 5)
            return {"state": "pending"} if not candidates and waiting else unavailable
        event = candidates[0]
        return {"state": "available", "source": "codex_history", "recorded_at": event["at"],
                **{k: event[k] for k in ("input_tokens", "output_tokens", "used_tokens", "window_tokens")}}

    def lookup_ready(self, packet, next_created=None):
        """Read consecutive bounded chunks without waiting for the browser's next poll."""
        if not is_token_request(packet):
            return {"state": "unavailable"}
        usage = packet.get("upstream_usage")
        if (isinstance(usage, dict) and packet.get("status") == 200
                and packet.get("state") in {"streaming", "complete"}
                and all(type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens", "total_tokens"))):
            # Native history may contain the deliberately elevated client value.
            # Use it only to resolve the window, never to replace actual usage.
            native = self.lookup(packet, next_created)
            return {"state": "available", "source": "upstream_response",
                    "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                    "used_tokens": usage["total_tokens"], "window_tokens": native.get("window_tokens"),
                    "recorded_at": native.get("recorded_at"),
                    "window_pending": native.get("state") in {"pending", "loading"},
                    "usage_override_applied": bool(packet.get("usage_override_applied")),
                    "reported_input_tokens": (packet.get("reported_usage") or {}).get("input_tokens")}
        if packet.get("usage_override_applied"):
            return {"state": "unavailable"}
        deadline = time.monotonic() + .12
        for _ in range(8):
            result = self.lookup(packet, next_created)
            if result.get("state") != "loading" or time.monotonic() >= deadline:
                return result
        return result
