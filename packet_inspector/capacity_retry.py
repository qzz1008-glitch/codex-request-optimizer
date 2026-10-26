"""Bounded retries for explicit model-capacity failures before output is delivered."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import queue
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError

MAX_RETRIES = 3
RETRY_LIMIT = 9
RETRY_INTERVAL = 50
# The fixed local entry waits 600 seconds for response headers. Leave time for
# returning the last real capacity error, even after slow upstream attempts.
MAX_RETRY_SECONDS = 570
MIN_ATTEMPT_SECONDS = 10
UPSTREAM_TIMEOUT = 600
MAX_PROBE_BYTES = 1024 * 1024
CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
CAPACITY_CODES = {"server_is_overloaded", "server_overloaded", "model_overloaded", "model_at_capacity"}
HANDSHAKE_EVENTS = {"response.created", "response.in_progress", "response.queued", "ping", "keepalive"}
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete", "error"}
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
                return max(RETRY_INTERVAL, delay)
        except (ValueError, TypeError):
            pass
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(RETRY_INTERVAL, (target - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return RETRY_INTERVAL


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


def stream_event_class(payload):
    """Classify protocol structure, never user text or opaque metadata values.

    Output evidence wins over labels, including empty output-item lifecycles.
    Metadata extensions stay opaque; other unknown semantics are passed through
    rather than treated as proof that replay is safe. This does not identify or
    broaden the capacity errors eligible for retry.
    """
    if not isinstance(payload, dict):
        return "unknown"
    response = payload.get("response")
    if payload.get("output") or isinstance(response, dict) and response.get("output"):
        return "output"
    if any(key in payload for key in ("item", "part", "delta", "output_index")):
        return "output"
    kind = payload.get("type")
    if isinstance(kind, str):
        # Output families and positions also cover new item/media/tool types.
        if (kind.endswith(".delta") or kind.startswith(("response.output_", "response.content_part.",
                "response.reasoning_", "response.function_call_", "response.custom_tool_call_"))
                or kind.startswith("response.") and any(part.endswith("_call") for part in kind.split("."))):
            return "output"
    if isinstance(payload.get("error"), dict) or isinstance(kind, str) and kind in TERMINAL_EVENTS:
        return "terminal"
    status = payload.get("status")
    if not kind and isinstance(status, str) and status in {"completed", "failed", "incomplete"}:
        return "terminal"
    if not isinstance(kind, str):
        return "unknown"
    if kind in HANDSHAKE_EVENTS:
        return "control"
    # Codex consumes these as response metadata, not generated output. Match the
    # family so new metadata subevents/fields do not need individual allowlists.
    native_kind = kind.removeprefix("codex.")
    if native_kind == "response.metadata" or native_kind.startswith("response.metadata."):
        return "control"
    return "unknown"


def inspect_prefix(response, *, on_event=None):
    """Hold control events; output or unknown semantics commit the original bytes."""
    def done(prefix, overloaded, reason, payload=None):
        payload = payload if isinstance(payload, dict) else {}
        error = (payload.get("response") or {}).get("error") if isinstance(payload.get("response"), dict) else None
        error = error or payload.get("error") or (payload if payload.get("type") == "error" else {})
        notify(on_event, "probe_finished", reason=reason, capacity=capacity_error(payload, include_output=True),
               event_type=payload.get("type"), error_code=error.get("code") if isinstance(error, dict) else None,
               event_class=stream_event_class(payload), probe_bytes=len(prefix), **event_shape(payload))
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
            overloaded = stream_event_class(payload) != "output" and capacity_error(payload)
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
            event_class = stream_event_class(payload)
            if event_class == "output":
                native_response = payload.get("response")
                existing = payload.get("output") or isinstance(native_response, dict) and native_response.get("output")
                return done(prefix, False, "existing_output" if existing else "stream_committed", payload)
            if capacity_error(payload):
                return done(prefix, True, "capacity_error", payload)
            if event_class != "control":
                return done(prefix, False, "unknown_event" if event_class == "unknown" else "not_capacity", payload)
            notify(on_event, "control_event", event_type=payload.get("type"), event_class=event_class)
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


def _open(opener, request, timeout, on_event):
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    notify(on_event, "response_headers", status=response.status,
           content_type=response.headers.get("Content-Type", "").split(";", 1)[0],
           content_encoding=response.headers.get("Content-Encoding", "identity"),
           upstream_request_id=response.headers.get("X-Request-Id", ""))
    return response


def _response_socket(response):
    stream = response
    for _ in range(5):
        if stream is None:
            break
        sock = getattr(stream, "_sock", None)
        if sock is not None:
            return sock
        stream = getattr(stream, "fp", None) or getattr(stream, "raw", None)
    return None


class _RetryAttempt:
    """Bound a retry's open AND prefix read without changing the original request.

    urllib's timeout is a socket inactivity limit, not a wall-clock deadline.
    Only retries use this worker: a known, untouched capacity response remains
    available to return if this attempt exceeds the remaining header budget.
    Diagnostic callbacks are drained by the caller, never by an abandoned worker.
    """

    def __init__(self, opener, request, timeout, attempt):
        self.events = queue.SimpleQueue()
        self.done, self.abandoned = threading.Event(), threading.Event()
        self.guard = threading.Lock()
        self.response = self.result = self.error = None
        self.phase = "open"
        self.thread = threading.Thread(target=self._run, args=(opener, request, timeout, attempt),
                                       name="capacity-retry", daemon=True)
        self.thread.start()

    def record(self, event, **fields):
        if not self.abandoned.is_set():
            self.events.put((event, fields))

    def _run(self, opener, request, timeout, attempt):
        response = None
        try:
            self.record("attempt_started", attempt=attempt, timeout_ms=round(timeout * 1000))
            response = _open(opener, request, timeout, self.record)
            with self.guard:
                self.response = response
            if self.abandoned.is_set():
                return
            self.phase = "inspect"
            prefix, overloaded = inspect_prefix(response, on_event=self.record)
            # Once selected, an ordinary response stream keeps its original
            # network timeout; the retry budget must not truncate generation.
            sock = _response_socket(response)
            if sock is not None and not self.abandoned.is_set():
                sock.settimeout(UPSTREAM_TIMEOUT)
            self.result = BufferedResponse(response, prefix), overloaded
        except BaseException as exc:
            self.error = exc
        finally:
            try:
                if response is not None and (self.error is not None or self.abandoned.is_set()):
                    response.close()
            finally:
                self.done.set()

    def drain(self, on_event):
        while True:
            try:
                event, fields = self.events.get_nowait()
            except queue.Empty:
                return
            notify(on_event, event, **fields)

    def abandon(self):
        self.abandoned.set()
        with self.guard:
            response = self.response
        # close() can wait on the buffered reader's lock. Interrupt the socket
        # first; its owning worker closes the response when the read unwinds.
        sock = _response_socket(response)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.done.is_set() and self.result is not None:
            self.result[0].close()

    def receive(self, deadline, cancelled, still_enabled, on_event):
        selected = False
        try:
            while True:
                self.drain(on_event)
                if cancelled():
                    raise BrokenPipeError("Client disconnected during capacity retry")
                if not still_enabled():
                    return None, "disabled_during_attempt"
                if time.monotonic() >= deadline:
                    return None, "time_budget"
                if self.done.is_set():
                    self.drain(on_event)
                    if self.error is not None:
                        error = self.error.reason if isinstance(self.error, URLError) else self.error
                        # Socket timeouts use the remaining budget but can fire
                        # slightly before the monotonic deadline (OS rounding).
                        # Return the known capacity error rather than a new 502.
                        if isinstance(error, TimeoutError):
                            return None, "time_budget"
                        raise self.error
                    selected = True
                    return self.result, None
                self.done.wait(min(.1, max(0, deadline - time.monotonic())))
        finally:
            if not selected:
                self.abandon()


def open_response(opener, request, *, enabled=False, max_retries=MAX_RETRIES, on_retry=None, cancelled=lambda: False,
                  still_enabled=lambda: True, wait=wait_for_retry, on_event=None, deadline=None):
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 1 <= max_retries <= RETRY_LIMIT:
        raise ValueError("max_retries must be an integer from 1 to 9")
    now = time.monotonic()
    deadline = min(now + MAX_RETRY_SECONDS, deadline) if deadline is not None else now + MAX_RETRY_SECONDS
    notify(on_event, "retry_policy", budget_ms=MAX_RETRY_SECONDS * 1000, interval_ms=RETRY_INTERVAL * 1000,
           remaining_ms=round(max(0, deadline - now) * 1000))
    response, phase = None, "open"
    try:
        if cancelled():
            notify(on_event, "cancelled", reason="client_disconnected", phase="before_open")
            raise BrokenPipeError("Client disconnected before capacity retry")
        # Do not shorten an ordinary slow generation. The hard retry deadline
        # applies only after an explicit capacity failure has been received.
        notify(on_event, "attempt_started", attempt=1, timeout_ms=UPSTREAM_TIMEOUT * 1000)
        response = _open(opener, request, UPSTREAM_TIMEOUT, on_event)
        if not enabled:
            notify(on_event, "response_selected", reason="disabled")
            return response
        phase = "inspect"
        prefix, overloaded = inspect_prefix(response, on_event=on_event)
        response = BufferedResponse(response, prefix)
        for attempt in range(max_retries + 1):
            if not overloaded:
                notify(on_event, "response_selected", reason="probe_passed_through")
                return response
            if attempt == max_retries:
                notify(on_event, "response_selected", reason="retry_limit")
                return response
            if not still_enabled():
                notify(on_event, "response_selected", reason="setting_disabled")
                return response
            delay = retry_delay(response.headers, attempt + 1)
            if delay + MIN_ATTEMPT_SECONDS > deadline - time.monotonic():
                notify(on_event, "response_selected", reason="time_budget", phase="before_wait",
                       remaining_ms=round(max(0, deadline - time.monotonic()) * 1000),
                       delay_ms=round(delay * 1000) if delay < 1e12 else None)
                return response
            notify(on_event, "retry_scheduled", retry=attempt + 1, delay_ms=round(delay * 1000),
                   delay_source="retry_after" if delay > RETRY_INTERVAL else "fixed_interval")
            phase = "retry_callback"
            if on_retry:
                on_retry(attempt + 1, delay)
            phase = "wait"
            wait(delay, cancelled)
            if not still_enabled():
                notify(on_event, "response_selected", reason="disabled_during_wait")
                return response
            remaining = deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_SECONDS:
                notify(on_event, "response_selected", reason="time_budget", phase="after_wait",
                       remaining_ms=round(max(0, remaining) * 1000))
                return response
            phase = "retry_attempt"
            pending = _RetryAttempt(opener, request, remaining, attempt + 2)
            result, reason = pending.receive(deadline, cancelled, still_enabled, on_event)
            if result is None:
                notify(on_event, "response_selected", reason=reason, phase=pending.phase, status=response.status,
                       error_type=type(pending.error).__name__ if pending.error is not None else "",
                       remaining_ms=round(max(0, deadline - time.monotonic()) * 1000))
                return response
            previous = response
            response, overloaded = result
            previous.close()
        raise AssertionError("Capacity retry loop exhausted without a response")
    except BaseException as exc:
        notify(on_event, "cancelled" if isinstance(exc, (BrokenPipeError, ConnectionResetError)) else "attempt_error",
               error_type=type(exc).__name__, phase=phase)
        if response is not None:
            response.close()
        raise
