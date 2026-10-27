"""Supply retry advice to Codex; never wait, replay requests, or run a retry loop."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import json
import math

from .response_events import capacity_error, notify, MAX_OBSERVATION_BYTES
from .usage_trigger import UsageStream

DEFAULT_RETRIES = 10
MAX_RETRIES = 99
DEFAULT_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 99


def header_delay(headers):
    """Honor both numeric and HTTP-date advice, without persisting header values."""
    if not hasattr(headers, "items"):
        return 0
    values = [v for k, v in headers.items() if k.lower() == "retry-after"]
    delay = 0
    for value in values:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                delay = max(delay, math.ceil(seconds))
                continue
        except (ValueError, TypeError, OverflowError):
            pass
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delay = max(delay, math.ceil((target - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


def advise(payload, seconds, headers, record):
    if not capacity_error(payload, include_output=True):
        return False
    kind = payload.get("type")
    if kind == "response.failed":
        error = payload["response"]["error"]
    else:
        error = payload.get("error", payload)
    current = error.get("headers")
    if current is not None and not isinstance(current, dict):
        return False
    delay = max(seconds, header_delay(headers), header_delay(current or {}))
    updated = {k: v for k, v in (current or {}).items() if k.lower() != "retry-after"}
    updated["Retry-After"] = str(delay)
    error["headers"] = updated
    try:
        record(native_retry_advised=True, native_retry_delay_ms=delay * 1000)
    except Exception:
        # Capture/diagnostic failures must not interrupt the forwarded response.
        pass
    return True


class NativeRetryStream(UsageStream):
    """Rewrite one bounded SSE event at a time, including errors after output."""
    def __init__(self, response, record, *, rewrite_usage=False, policy=None, on_event=None):
        super().__init__(response, record, rewrite=rewrite_usage)
        self.policy, self.on_event = policy, on_event
        self.rewrite_response = rewrite_usage or policy is not None

    def _rewrite(self, value, event=False):
        changed = super()._rewrite(value, event=event)
        if self.policy is None or not isinstance(value, dict):
            return changed
        if event and value.get("type") not in {"error", "response.failed"}:
            return changed
        enabled, seconds = self.policy()
        if enabled:
            def record(**fields):
                self.record(**fields)
                notify(self.on_event, "native_retry_advised", delay_ms=fields["native_retry_delay_ms"])
            advised = advise(value, seconds, self.response.headers, record)
            if advised and event and value.get("type") == "error":
                # Codex consumes retry advice on response.failed. Bare error
                # events are not a portable capacity-error retry interface.
                error = value.get("error", {k: v for k, v in value.items() if k != "type"})
                response = value.get("response")
                value["response"] = {**(response if isinstance(response, dict) else {}), "error": deepcopy(error)}
                value["type"] = "response.failed"
            changed |= advised
        return changed


class PrefixResponse:
    """A bounded inspected HTTP error body followed by the untouched upstream."""
    def __init__(self, response, prefix, *, changed=False):
        self.response, self.prefix = response, io.BytesIO(prefix)
        self.status, self.reason = response.status, response.reason
        self.headers = deepcopy(response.headers)
        self.changed = changed

    def read1(self, size=65536):
        return self.prefix.read(size) or self.response.read(size)

    def read(self, size=-1):
        if size < 0:
            return self.prefix.read() + self.response.read()
        prefix = self.prefix.read(size)
        return prefix + self.response.read(size - len(prefix)) if len(prefix) < size else prefix

    def close(self):
        self.prefix.close()
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def prepare_http_error(response, policy, record, on_event=None):
    """Classify a small HTTP error before headers; successful streams are untouched."""
    if policy is None or response.status not in {429, 500, 502, 503, 504}:
        return response
    enabled, seconds = policy()
    kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if not enabled or encoding not in {"", "identity"} or kind not in {"application/json", ""} and not kind.endswith("+json"):
        return response
    try:
        prefix = response.read(MAX_OBSERVATION_BYTES + 1)
    except BaseException:
        response.close()
        raise
    prepared = PrefixResponse(response, prefix)
    if len(prefix) > MAX_OBSERVATION_BYTES:
        return prepared
    try:
        value = json.loads(prefix)
    except (ValueError, UnicodeError):
        return prepared
    advice = {}
    if not advise(value, seconds, response.headers, lambda **fields: advice.update(fields)):
        return prepared
    prepared.prefix.close()
    prepared.prefix = io.BytesIO(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8"))
    prepared.changed = True
    for key in ("Retry-After", "Content-Length", "Content-MD5", "Digest", "ETag"):
        if key in prepared.headers:
            del prepared.headers[key]
    prepared.headers["Retry-After"] = str(advice["native_retry_delay_ms"] // 1000)
    prepared.headers["Content-Length"] = str(len(prepared.prefix.getbuffer()))
    try:
        record(**advice)
    except Exception:
        pass
    notify(on_event, "native_retry_advised", delay_ms=advice["native_retry_delay_ms"])
    return prepared
