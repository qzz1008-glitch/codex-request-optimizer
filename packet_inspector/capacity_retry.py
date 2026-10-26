"""Bounded retries for explicit model-capacity failures before output is delivered."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import random
import re
import time
from urllib.error import HTTPError

MAX_RETRIES = 3
MAX_RETRY_SECONDS = 90
MAX_PROBE_BYTES = 1024 * 1024
CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
CAPACITY_CODES = {"server_is_overloaded", "server_overloaded", "model_overloaded", "model_at_capacity"}
HANDSHAKE_EVENTS = {"response.created", "response.in_progress", "response.queued", "ping", "keepalive"}
EVENT_BOUNDARY = re.compile(br"\r?\n\r?\n")


class BufferedResponse:
    """Replay the inspected prefix exactly, then continue the original response."""

    def __init__(self, response, prefix=b""):
        self.response = response
        self.prefix = bytearray(prefix)
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers

    def read1(self, size=65536):
        if self.prefix:
            result = bytes(self.prefix[:size])
            del self.prefix[:size]
            return result
        return getattr(self.response, "read1", self.response.read)(size)

    def read(self, size=-1):
        if size < 0:
            result = bytes(self.prefix)
            self.prefix.clear()
            return result + self.response.read()
        return self.read1(size)

    def close(self):
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def capacity_error(payload, *, include_output=False):
    if not isinstance(payload, dict):
        return False
    response = payload.get("response")
    if not include_output and (payload.get("output") or isinstance(response, dict) and response.get("output")):
        return False
    error = payload.get("error")
    if payload.get("type") == "response.failed":
        error = response.get("error") if isinstance(response, dict) else None
    elif payload.get("type") == "error" and error is None:
        error = payload
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    return isinstance(code, str) and code in CAPACITY_CODES or (
        isinstance(error.get("message"), str)
        and error["message"].strip().casefold() == CAPACITY_MESSAGE.casefold()
    )


def retry_delay(headers, attempt, now=None):
    value = headers.get("Retry-After")
    if value is not None:
        try:
            delay = float(value)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0, (target - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return min(30, 2 ** attempt * random.uniform(1, 1.25))


def notify(callback, event, **fields):
    """Diagnostics cannot change forwarding or retry decisions."""
    if callback:
        try:
            callback(event, **fields)
        except Exception:
            pass


def event_shape(payload):
    """Structural evidence only: no text, arguments, ciphertext or item IDs."""
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        return {}
    return {"item_type": item.get("type"),
            "summary_items": len(item["summary"]) if isinstance(item.get("summary"), list) else 0,
            "content_items": len(item["content"]) if isinstance(item.get("content"), list) else 0,
            "has_encrypted_content": bool(item.get("encrypted_content"))}


def inspect_prefix(response, *, on_event=None):
    """Only empty/handshake events may be held; the first output commits the stream."""
    def done(prefix, overloaded, reason, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        error = (payload.get("response") or {}).get("error") if isinstance(payload.get("response"), dict) else None
        error = error or payload.get("error") or (payload if payload.get("type") == "error" else {})
        notify(on_event, "probe_finished", reason=reason, capacity=capacity_error(payload, include_output=True),
               event_type=payload.get("type"), error_code=error.get("code") if isinstance(error, dict) else None,
               probe_bytes=len(prefix), **event_shape(payload))
        return bytes(prefix), overloaded

    if response.status not in {200, 429, 500, 502, 503, 504}:
        return done(b"", False, "unsupported_status")
    if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
        return done(b"", False, "encoded_response")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
    if content_type and content_type not in {"application/json", "text/event-stream"}:
        return done(b"", False, "unsupported_content_type")
    mode = "sse" if content_type == "text/event-stream" else "json" if content_type == "application/json" else None
    prefix, offset = bytearray(), 0
    reader = getattr(response, "read1", response.read)
    while len(prefix) < MAX_PROBE_BYTES:
        chunk = reader(min(65536, MAX_PROBE_BYTES - len(prefix)))
        if not chunk:
            return done(prefix, False, "end_before_terminal")
        prefix.extend(chunk)
        if mode is None:
            beginning = bytes(prefix).lstrip()
            if beginning.startswith((b"data:", b"event:", b":", b"id:", b"retry:")):
                mode = "sse"
            elif beginning.startswith(b"{"):
                mode = "json"
            elif len(beginning) >= 8:
                return done(prefix, False, "unknown_format")
            else:
                continue
        if mode == "json":
            try:
                payload = json.loads(prefix)
            except (ValueError, UnicodeError):
                continue
            overloaded = capacity_error(payload)
            return done(prefix, overloaded, "capacity_error" if overloaded else "not_capacity", payload)
        while (boundary := EVENT_BOUNDARY.search(prefix, offset)) is not None:
            frame = bytes(prefix[offset:boundary.start()])
            offset = boundary.end()
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:"))
            if not data:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                return done(prefix, False, "malformed_event")
            if isinstance(payload, dict) and not payload.get("type"):
                event_names = [line[6:].strip() for line in frame.splitlines() if line.startswith(b"event:")]
                if event_names:
                    payload = {**payload, "type": event_names[-1].decode("utf8", errors="replace")}
            if capacity_error(payload):
                return done(prefix, True, "capacity_error", payload)
            if (not isinstance(payload, dict) or not isinstance(payload.get("type"), str)
                    or payload["type"] not in HANDSHAKE_EVENTS):
                return done(prefix, False, "stream_committed", payload)
            native_response = payload.get("response")
            if isinstance(native_response, dict) and native_response.get("output"):
                return done(prefix, False, "existing_output", payload)
    return done(prefix, False, "probe_limit")


def wait_for_retry(delay, cancelled):
    deadline = time.monotonic() + delay
    while True:
        if cancelled():
            raise BrokenPipeError("Client disconnected during capacity retry")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.2, remaining))


def open_response(opener, request, *, enabled=False, max_retries=MAX_RETRIES, on_retry=None, cancelled=lambda: False,
                  still_enabled=lambda: True, wait=wait_for_retry, on_event=None):
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 1 <= max_retries <= 99:
        raise ValueError("max_retries must be an integer from 1 to 99")
    deadline = time.monotonic() + MAX_RETRY_SECONDS
    for attempt in range(max_retries + 1):
        if cancelled():
            notify(on_event, "cancelled", reason="client_disconnected", phase="before_open")
            raise BrokenPipeError("Client disconnected before capacity retry")
        notify(on_event, "attempt_started", attempt=attempt + 1)
        try:
            response = opener.open(request, timeout=600)
        except HTTPError as exc:
            response = exc
        except Exception as exc:
            notify(on_event, "attempt_error", error_type=type(exc).__name__, phase="open")
            raise
        notify(on_event, "response_headers", status=response.status,
               content_type=response.headers.get("Content-Type", "").split(";", 1)[0],
               content_encoding=response.headers.get("Content-Encoding", "identity"),
               upstream_request_id=response.headers.get("X-Request-Id", ""))
        if not enabled:
            notify(on_event, "response_selected", reason="disabled")
            return response
        phase = "inspect"
        try:
            prefix, overloaded = inspect_prefix(response, on_event=on_event)
            buffered = BufferedResponse(response, prefix)
            if not overloaded:
                notify(on_event, "response_selected", reason="probe_passed_through")
                return buffered
            if attempt == max_retries:
                notify(on_event, "response_selected", reason="retry_limit")
                return buffered
            if not still_enabled():
                notify(on_event, "response_selected", reason="setting_disabled")
                return buffered
            delay = retry_delay(response.headers, attempt + 1)
            if delay > deadline - time.monotonic():
                notify(on_event, "response_selected", reason="time_budget",
                       delay_ms=round(delay * 1000) if delay < 1e12 else None)
                return buffered
            notify(on_event, "retry_scheduled", retry=attempt + 1, delay_ms=round(delay * 1000),
                   delay_source="retry_after" if response.headers.get("Retry-After") is not None else "backoff")
            if on_retry:
                phase = "retry_callback"
                on_retry(attempt + 1, delay)
            phase = "wait"
            wait(delay, cancelled)
            if not still_enabled():
                notify(on_event, "response_selected", reason="disabled_during_wait")
                return buffered
        except BaseException as exc:
            notify(on_event, "attempt_error", error_type=type(exc).__name__, phase=phase)
            response.close()
            raise
        response.close()
    raise AssertionError("Capacity retry loop exhausted without a response")
