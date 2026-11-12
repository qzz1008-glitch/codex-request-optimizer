"""Retry only a small, generic HTTP 400 before exposing any response to Codex."""
from __future__ import annotations

import json
import time

from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .native_retry import PrefixResponse, header_delay
from .response_events import notify

DEFAULT_RETRIES = 1
MAX_RETRIES = 9
MAX_ERROR_BYTES = 4096
RETRY_DELAY_SECONDS = 1
MAX_RETRY_SECONDS = 60
RESPONSE_RESERVE_SECONDS = 5


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def inspect_bad_request(response):
    """Return a replayable viewing prefix, never drop bytes from another error."""
    if response.status != 400:
        return response, None
    kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"} or kind not in {"", "application/json"} and not kind.endswith("+json"):
        return response, None
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if not 0 <= int(length) <= MAX_ERROR_BYTES:
                return response, None
        except ValueError:
            return response, None
    try:
        prefix = response.read(MAX_ERROR_BYTES + 1)
    except BaseException:
        response.close()
        raise
    prepared = PrefixResponse(response, prefix)
    if len(prefix) > MAX_ERROR_BYTES:
        return prepared, None
    try:
        value = json.loads(prefix, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        return prepared, None
    if (not isinstance(value, dict) or set(value) != {"detail"}
            or not isinstance(value["detail"], str) or value["detail"].strip().casefold() != "bad request"):
        return prepared, None
    return prepared, prefix


def retry_bad_request(response, open_retry, *, max_retries, started, enabled,
                      before_retry, record, on_event=None, clock=time.monotonic, wait=time.sleep):
    """Replay the same request only after a complete, explicitly rejected response.

    The extra attempts share a 60-second budget, also bounded by the original
    client's response wait. A successful stream keeps the normal idle timeout.
    """
    deadline = min(started + UPSTREAM_READ_TIMEOUT_SECONDS - RESPONSE_RESERVE_SECONDS,
                   clock() + MAX_RETRY_SECONDS)
    retries = 0
    while True:
        response, rejected = inspect_bad_request(response)
        if rejected is None:
            if retries:
                restore = getattr(response, "inspector_set_read_timeout", None) if response.status != 400 else None
                if restore:
                    restore(UPSTREAM_READ_TIMEOUT_SECONDS)
                notify(on_event, "bad_request_retry_finished", retry=retries, status=response.status)
            return response
        notify(on_event, "bad_request_detected", retry=retries, status=400)
        delay = max(RETRY_DELAY_SECONDS, header_delay(response.headers))
        reason = ("disabled" if not enabled() else "retry_limit" if retries >= max_retries
                  else "time_budget" if clock() + delay >= deadline else "")
        if reason:
            notify(on_event, "bad_request_retry_stopped", reason=reason, retry=retries)
            return response
        notify(on_event, "bad_request_retry_scheduled", retry=retries + 1, delay_ms=delay * 1000,
               remaining_ms=max(0, round((deadline - clock()) * 1000)))
        try:
            wait(delay)
            remaining = deadline - clock()
            if not enabled() or remaining <= 0:
                notify(on_event, "bad_request_retry_stopped", retry=retries,
                       reason="disabled" if not enabled() else "time_budget")
                return response
            before_retry(rejected)
        except BaseException:
            response.close()
            raise
        response.close()
        retries += 1
        record(bad_request_retry_count=retries)
        response = open_retry(retries, min(remaining, UPSTREAM_READ_TIMEOUT_SECONDS))
