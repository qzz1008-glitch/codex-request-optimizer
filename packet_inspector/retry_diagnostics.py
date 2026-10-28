"""Small, bounded retry audit records, independent of captured request bodies."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .response_events import (EVENT_BOUNDARY, MAX_OBSERVATION_BYTES, TERMINAL_EVENTS as TERMINALS,
                             capacity_error, event_shape, notify, stream_event_class)
from .maintenance import SQLiteReclaimer

RETENTION_DAYS = 30
MAX_RECORDS = 20000
MAX_BYTES = 32 * 1024 * 1024
EVENT_FIELDS = {"attempt", "retry", "reason", "phase", "status", "content_type", "content_encoding",
                "event_type", "event_class", "error_code", "error_type", "capacity", "probe_bytes", "delay_ms", "delay_source",
                "item_type", "summary_items", "content_items", "has_encrypted_content", "upstream_request_id",
                "budget_ms", "interval_ms", "remaining_ms", "timeout_ms"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/+-]{1,160}\Z")
TYPE_FIELD = re.compile(rb'"type"\s*:\s*"([a-z_.]+)"')


def identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else ""


class RetryJournal:
    def __init__(self, directory, *, retention_days=RETENTION_DAYS, max_records=MAX_RECORDS,
                 max_bytes=MAX_BYTES, normal_records=2000, clock=time.time):
        self.path = Path(directory) / "retry-diagnostics.sqlite"
        self.retention_days, self.max_records, self.max_bytes = retention_days, max_records, max_bytes
        self.normal_records, self.clock = normal_records, clock
        self.lock = threading.RLock()
        self.errors, self.last_error, self.next_prune = 0, "", 0
        self.reclaimer = SQLiteReclaimer(self.path, self.lock)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=.05)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _schema(db):
        db.execute("""CREATE TABLE IF NOT EXISTS retry_requests (
            id TEXT PRIMARY KEY, packet_id TEXT, session_id TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, finished INTEGER NOT NULL, significant INTEGER NOT NULL,
            bytes INTEGER NOT NULL, document TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS retry_session ON retry_requests(session_id,created DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS retry_packet ON retry_requests(packet_id)")

    def _error(self, error):
        self.errors += 1
        self.last_error = type(error).__name__

    def begin(self, *, packet_id=None, session_id="", endpoint="responses", enabled=False,
              eligible=True, max_retries=3, on_summary=None):
        return RetryTrace(self, packet_id=packet_id, session_id=session_id, endpoint=endpoint,
                          enabled=enabled, eligible=eligible, max_retries=max_retries, on_summary=on_summary)

    def save(self, document):
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        significant = (document["capacity_error_seen"] or document["retry_count"] > 0
                       or document["outcome"] not in {"pending", "success"})
        try:
            with self.lock, self._connect() as db:
                self._schema(db)
                db.execute("INSERT OR REPLACE INTO retry_requests VALUES(?,?,?,?,?,?,?,?,?)",
                           (document["id"], document["packet_id"], document["session_id"], document["created"],
                            document["updated"], int(document["state"] == "finished"), int(significant),
                            len(raw.encode("utf8")), raw))
            return True
        except (OSError, sqlite3.Error) as error:
            self._error(error)
            return False

    def prune(self, *, force=False):
        now = self.clock()
        if not self.path.exists() or not force and now < self.next_prune:
            return
        try:
            with self.lock, self._connect() as db:
                self._schema(db)
                # A process can disappear before finish(). Preserve that fact;
                # never turn an abandoned trace into an invented success.
                db.execute("""UPDATE retry_requests SET finished=1,significant=1,
                    document=json_set(document,'$.state','finished','$.outcome','unfinished',
                                      '$.stop_reason','no_final_record')
                    WHERE finished=0 AND updated<?""", (now - 6 * 3600,))
                db.execute("DELETE FROM retry_requests WHERE finished=1 AND created<?",
                           (now - self.retention_days * 86400,))
                db.execute("DELETE FROM retry_requests WHERE finished=1 AND significant=0 AND created<?",
                           (now - 86400,))
                db.execute("""DELETE FROM retry_requests WHERE id IN (
                    SELECT id FROM retry_requests WHERE finished=1 AND significant=0
                    ORDER BY created DESC LIMIT -1 OFFSET ?)""", (self.normal_records,))
                count, size = db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM retry_requests").fetchone()
                if count > self.max_records or size > self.max_bytes:
                    rows = db.execute("SELECT id,bytes FROM retry_requests WHERE finished=1 ORDER BY significant,created")
                    for row in rows.fetchall():
                        if count <= self.max_records and size <= self.max_bytes:
                            break
                        db.execute("DELETE FROM retry_requests WHERE id=?", (row["id"],))
                        count -= 1
                        size -= row["bytes"]
                changed = db.total_changes
            self.next_prune = now + 60
            if changed:
                self.reclaimer.request()
        except (OSError, sqlite3.Error) as error:
            self._error(error)
            self.next_prune = now + 60

    def get(self, request_id):
        if not self.path.exists():
            raise KeyError(request_id)
        with self.lock, self._connect() as db:
            row = db.execute("SELECT document FROM retry_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        return json.loads(row[0])

    def available(self, request_id):
        if not self.path.exists():
            return False
        try:
            with self.lock, self._connect() as db:
                return db.execute("SELECT 1 FROM retry_requests WHERE id=?", (request_id,)).fetchone() is not None
        except (OSError, sqlite3.Error) as error:
            self._error(error)
            return None

    def list(self, *, session_id="", packet_id="", limit=50, before=None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not self.path.exists():
            return []
        clauses, values = [], []
        for key, value in (("session_id", session_id), ("packet_id", packet_id)):
            if value:
                clauses.append(key + "=?")
                values.append(value)
        if before is not None:
            clauses.append("created<?")
            values.append(before)
        sql = "SELECT document FROM retry_requests" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self.lock, self._connect() as db:
            rows = db.execute(sql + " ORDER BY created DESC,id DESC LIMIT ?", (*values, limit)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != "events"} for row in rows]


class RetryTrace:
    def __init__(self, journal, *, packet_id, session_id, endpoint, enabled, eligible, max_retries, on_summary):
        self.journal, self.on_summary = journal, on_summary
        now = journal.clock()
        self.document = {"version": 2, "mode": "native", "id": packet_id or uuid.uuid4().hex, "packet_id": packet_id,
                         "session_id": session_id if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id) else "",
                         "endpoint": endpoint if endpoint in {"responses", "compact"} else "other", "created": now, "updated": now,
                         "enabled": bool(enabled), "eligible": bool(eligible), "max_retries": max_retries,
                         "attempt_count": 0, "retry_count": 0, "scheduled_retries": 0,
                         "capacity_error_seen": False, "final_capacity_error": False,
                          "state": "active", "outcome": "pending",
                          "stop_reason": "endpoint_not_eligible" if not eligible else "disabled" if not enabled else "",
                          "events_dropped": 0, "events": []}
        self.saved = False
        self.record("request_started")
        journal.prune()

    def summary(self):
        keys = ("version", "id", "updated", "enabled", "eligible", "max_retries", "attempt_count", "retry_count",
                "scheduled_retries", "capacity_error_seen", "final_capacity_error", "state", "outcome",
                 "stop_reason", "probe_reason", "http_status", "first_output_event", "terminal_event", "observation_reason",
                 "native_retry_advised", "native_retry_delay_ms")
        return {**{key: self.document[key] for key in keys if key in self.document}, "journal_saved": self.saved}

    def record(self, event, **fields):
        # Never save arbitrary headers, error messages, payloads, URLs or output.
        safe = {key: value for key, value in fields.items() if key in EVENT_FIELDS
                and (isinstance(value, (bool, int, float)) or isinstance(value, str) and identifier(value))}
        d = self.document
        d["updated"] = self.journal.clock()
        entry = {"time": d["updated"], "event": event, "attempt": d["attempt_count"], **safe}
        if len(d["events"]) >= 512:
            # Keep the start and newest evidence, including final outcomes.
            del d["events"][1]
            d["events_dropped"] += 1
        d["events"].append(entry)
        if event == "attempt_started":
            d["attempt_count"] = fields["attempt"]
            d["retry_count"] = max(0, fields["attempt"] - 1)
            for key in ("terminal_event", "probe_reason", "http_status", "first_output_event", "observation_reason"):
                d.pop(key, None)
            d["final_capacity_error"] = False
        elif event == "native_retry_advised":
            d["native_retry_advised"] = True
            d["native_retry_delay_ms"] = fields["delay_ms"]
            d["stop_reason"] = "native_retry_delegated"
        elif event == "response_headers":
            d["http_status"] = fields["status"]
        elif event == "probe_finished":
            d["probe_reason"] = fields["reason"]
        elif event == "retry_scheduled":
            d["scheduled_retries"] = fields["retry"]
        elif event == "response_selected":
            if "status" in fields:
                d["http_status"] = fields["status"]
            reason = fields["reason"]
            if reason == "disabled" and not d["eligible"]:
                reason = "endpoint_not_eligible"
            d["stop_reason"] = (d.get("probe_reason", reason) if reason == "probe_passed_through" else reason)
        elif event == "first_output":
            d["first_output_event"] = safe.get("event_type", "")
        elif event == "terminal":
            d["terminal_event"] = safe.get("event_type", "")
            d["final_capacity_error"] = bool(fields.get("capacity"))
        elif event == "observation_stopped":
            d["observation_reason"] = fields["reason"]
        elif event in {"cancelled", "attempt_error"}:
            cancelled = (event == "cancelled" or fields.get("phase") == "wait" and fields.get("error_type") in
                         {"BrokenPipeError", "ConnectionResetError"})
            d["stop_reason"] = "client_disconnected" if cancelled else "upstream_exception"
        if fields.get("capacity"):
            d["capacity_error_seen"] = d["final_capacity_error"] = True
        self.saved = self.journal.save(d)
        if self.on_summary:
            try:
                self.on_summary(self.summary())
            except Exception as error:
                self.journal._error(error)

    def finish(self, state, *, error_type="", error_phase="", sent_headers=False, response_bytes=0):
        d = self.document
        if state == "client_disconnected" and (error_phase not in {"upstream_open", "upstream_read"}
                                               or d["stop_reason"] == "client_disconnected"):
            outcome = "cancelled"
        elif state in {"relay_error", "client_disconnected"}:
            outcome = "transport_error"
        elif d["final_capacity_error"]:
            outcome = "capacity_error"
        elif d.get("terminal_event") == "response.completed" and d.get("http_status") == 200:
            outcome = "success"
        elif d.get("terminal_event") in {"error", "response.failed", "response.incomplete"}:
            outcome = "upstream_error"
        elif d.get("http_status", 0) >= 400:
            outcome = "http_error"
        else:
            outcome = "unobserved"
        d.update(state="finished", outcome=outcome, transport_state=state,
                 sent_headers=bool(sent_headers), response_bytes=response_bytes)
        self.record("request_finished", error_type=error_type, phase=error_phase)
        self.journal.prune()


class ResponseObservation:
    """Observe only structural events in the final response; never change bytes."""
    def __init__(self, trace, headers, on_error=None):
        self.trace = trace
        self.on_error = on_error
        kind = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        self.mode = "sse" if kind == "text/event-stream" else "json" if kind == "application/json" else None
        self.buffer, self.dropping, self.output_seen = bytearray(), False, False
        self.unknown_seen = False
        self.warnings = set()
        self.stopped = False
        encoding = headers.get("Content-Encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            self.stop("encoded_response")
        elif kind and self.mode is None:
            self.stop("unsupported_content_type")

    def stop(self, reason):
        self.stopped = True
        self.buffer.clear()
        self.trace.record("observation_stopped", reason=reason)

    def warn(self, reason):
        if reason not in self.warnings:
            self.warnings.add(reason)
            self.trace.record("observation_stopped", reason=reason)

    def _payload(self, payload, event_name=""):
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") or event_name
        if not isinstance(event_type, str):
            return
        if event_name and not payload.get("type"):
            payload = {**payload, "type": event_name}
        if not event_type and isinstance(payload.get("status"), str) and payload["status"] in {"completed", "failed", "incomplete"}:
            event_type = "response." + payload["status"]
        if event_type in TERMINALS:
            response = payload.get("response")
            error = (response.get("error") if isinstance(response, dict) else None) or payload.get("error")
            error = error or (payload if event_type == "error" else {})
            if event_type in {"error", "response.failed", "response.incomplete"}:
                self._error(payload)
            self.trace.record("terminal", event_type=event_type,
                              capacity=capacity_error(payload, include_output=True),
                              error_code=error.get("code") if isinstance(error, dict) else None)
        elif not event_type and isinstance(payload.get("error"), dict):
            self._error(payload)
            self.trace.record("terminal", event_type="error", capacity=capacity_error(payload, include_output=True),
                              error_code=payload["error"].get("code"))
        event_class = stream_event_class(payload)
        if event_class == "output" and not self.output_seen:
            self.output_seen = True
            self.trace.record("first_output", event_type=event_type, event_class=event_class, **event_shape(payload))
        elif event_class == "unknown" and not self.unknown_seen:
            self.unknown_seen = True
            self.trace.record("unclassified_event", event_type=event_type, event_class=event_class)

    def _error(self, payload):
        if self.on_error:
            try:
                self.on_error(payload)
            except Exception:
                pass  # Diagnostic failure must not change a response.

    def feed(self, chunk):
        if self.stopped:
            return
        try:
            self._feed(chunk)
        except Exception as error:
            self.trace.journal._error(error)
            self.stopped = True
            self.buffer.clear()
            notify(self.trace.record, "observation_stopped", reason="observer_error")

    def _feed(self, chunk):
        self.buffer.extend(chunk)
        if self.mode is None:
            beginning = bytes(self.buffer).lstrip()
            if beginning.startswith((b"data:", b"event:", b":", b"id:", b"retry:")):
                self.mode = "sse"
            elif beginning.startswith(b"{"):
                self.mode = "json"
            elif len(beginning) >= 8:
                self.stop("unknown_format")
                return
        if self.mode == "sse":
            while (boundary := EVENT_BOUNDARY.search(self.buffer)) is not None:
                frame = bytes(self.buffer[:boundary.start()])
                del self.buffer[:boundary.end()]
                if len(frame) > MAX_OBSERVATION_BYTES:
                    self.warn("frame_limit")
                elif not self.dropping:
                    data = b"\n".join(line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:"))
                    names = [line[6:].strip().decode("utf8", errors="replace") for line in frame.splitlines() if line.startswith(b"event:")]
                    terminal_hint = (bool(names and names[-1] in TERMINALS) or
                                     any(match[1].decode() in TERMINALS for match in TYPE_FIELD.finditer(data)))
                    if data and data != b"[DONE]" and (terminal_hint or not self.output_seen):
                        try:
                            self._payload(json.loads(data), names[-1] if names else "")
                        except (ValueError, UnicodeError):
                            self.warn("malformed_event")
                self.dropping = False
        if len(self.buffer) > MAX_OBSERVATION_BYTES:
            if self.mode == "sse":
                self.warn("frame_limit")
                self.dropping = True
                del self.buffer[:-3]
            else:
                self.stop("body_limit")

    def finish(self):
        if not self.stopped and self.mode == "json" and self.buffer:
            try:
                self._payload(json.loads(self.buffer))
            except (ValueError, UnicodeError):
                self.warn("malformed_json")
        elif not self.stopped and self.mode == "sse" and self.buffer.strip():
            self.warn("incomplete_event")
        self.buffer.clear()
