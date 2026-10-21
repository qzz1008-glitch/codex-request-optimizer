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


def capacity_error(payload):
    if not isinstance(payload, dict):
        return False
    response = payload.get("response")
    if payload.get("output") or isinstance(response, dict) and response.get("output"):
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


def inspect_prefix(response):
    """Only empty/handshake events may be held; the first output commits the stream."""
    if response.status not in {200, 429, 500, 502, 503, 504}:
        return b"", False
    if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
        return b"", False
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
    if content_type and content_type not in {"application/json", "text/event-stream"}:
        return b"", False
    mode = "sse" if content_type == "text/event-stream" else "json" if content_type == "application/json" else None
    prefix, offset = bytearray(), 0
    reader = getattr(response, "read1", response.read)
    while len(prefix) < MAX_PROBE_BYTES:
        chunk = reader(min(65536, MAX_PROBE_BYTES - len(prefix)))
        if not chunk:
            return bytes(prefix), False
        prefix.extend(chunk)
        if mode is None:
            beginning = bytes(prefix).lstrip()
            if beginning.startswith((b"data:", b"event:", b":", b"id:", b"retry:")):
                mode = "sse"
            elif beginning.startswith(b"{"):
                mode = "json"
            elif len(beginning) >= 8:
                return bytes(prefix), False
            else:
                continue
        if mode == "json":
            try:
                payload = json.loads(prefix)
            except (ValueError, UnicodeError):
                continue
            return bytes(prefix), capacity_error(payload)
        while (boundary := EVENT_BOUNDARY.search(prefix, offset)) is not None:
            frame = bytes(prefix[offset:boundary.start()])
            offset = boundary.end()
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:"))
            if not data:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                return bytes(prefix), False
            if isinstance(payload, dict) and not payload.get("type"):
                event_names = [line[6:].strip() for line in frame.splitlines() if line.startswith(b"event:")]
                if event_names:
                    payload = {**payload, "type": event_names[-1].decode("utf8", errors="replace")}
            if capacity_error(payload):
                return bytes(prefix), True
            if not isinstance(payload, dict) or payload.get("type") not in HANDSHAKE_EVENTS:
                return bytes(prefix), False
            native_response = payload.get("response")
            if isinstance(native_response, dict) and native_response.get("output"):
                return bytes(prefix), False
    return bytes(prefix), False


def wait_for_retry(delay, cancelled):
    deadline = time.monotonic() + delay
    while True:
        if cancelled():
            raise BrokenPipeError("Client disconnected during capacity retry")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.2, remaining))


def open_response(opener, request, *, enabled=False, on_retry=None, cancelled=lambda: False,
                  still_enabled=lambda: True, wait=wait_for_retry):
    deadline = time.monotonic() + MAX_RETRY_SECONDS
    for attempt in range(MAX_RETRIES + 1):
        if cancelled():
            raise BrokenPipeError("Client disconnected before capacity retry")
        try:
            response = opener.open(request, timeout=600)
        except HTTPError as exc:
            response = exc
        if not enabled:
            return response
        try:
            prefix, overloaded = inspect_prefix(response)
            buffered = BufferedResponse(response, prefix)
            if not overloaded or attempt == MAX_RETRIES or not still_enabled():
                return buffered
            delay = retry_delay(response.headers, attempt + 1)
            if delay > deadline - time.monotonic():
                return buffered
            if on_retry:
                on_retry(attempt + 1, delay)
            wait(delay, cancelled)
            if not still_enabled():
                return buffered
        except BaseException:
            response.close()
            raise
        response.close()
    raise AssertionError("Capacity retry loop exhausted without a response")
