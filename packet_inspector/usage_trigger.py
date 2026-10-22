"""An explicit relay option to prompt native compaction through response usage."""
from __future__ import annotations

from copy import deepcopy
from itertools import chain
import json
import re
from urllib.parse import urlsplit

from .analyzer import classify_request, decode_body

DEFAULT_BUDGET_BYTES = 1_000_000
TOKEN_HEADROOM_BYTES = 50_000
MIN_BUDGET_BYTES = TOKEN_HEADROOM_BYTES + 1
TRIGGER_BYTES = DEFAULT_BUDGET_BYTES - TOKEN_HEADROOM_BYTES
REPORTED_INPUT_TOKENS = 500_000
MAX_EVENT_BYTES = 4 * 1024 * 1024
BOUNDARY = re.compile(br"\r?\n\r?\n")


def trigger_threshold(budget_bytes):
    return budget_bytes - TOKEN_HEADROOM_BYTES


def request_size(body, headers, method, path, wire_bytes, budget_bytes=DEFAULT_BUDGET_BYTES):
    """Measure the actual upload encoding; exclude native compaction calls."""
    threshold = trigger_threshold(budget_bytes)
    if (threshold <= 0 or wire_bytes < threshold or method != "POST"
            or not urlsplit(path).path.rstrip("/").endswith("/responses")):
        return None
    try:
        encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
        decoded = decode_body(body, encoding)
        value = json.loads(decoded)
        if not isinstance(value, dict) or "input" not in value:
            return None
        classification = classify_request(value, headers, path)
        if classification["is_compaction"] or classification.get("request_kind") in {"prewarm", "memory"}:
            return None
        return wire_bytes
    except Exception:
        return None


def raw_chunks(response):
    reader = getattr(response, "read1", response.read)
    while True:
        chunk = reader(65536)
        if not chunk:
            return
        yield chunk


class UsageStream:
    """Stream generation content and retain actual usage before rewriting usage."""
    def __init__(self, response, record):
        self.response, self.record = response, record
        self.mode = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        self.enabled = response.status == 200 and encoding in {"", "identity"} and self.mode in {
            "", "text/event-stream", "application/json"}

    def _rewrite(self, value, event=False):
        if not isinstance(value, dict):
            return False
        if event and value.get("type") != "response.completed":
            return False
        response = value.get("response") if event else value
        if not isinstance(response, dict) or response.get("status") in {"failed", "incomplete", "cancelled"}:
            return False
        usage = response.get("usage")
        if not isinstance(usage, dict) or not all(type(usage.get(k)) is int and usage[k] >= 0
                for k in ("input_tokens", "output_tokens", "total_tokens")):
            return False
        original = deepcopy(usage)
        changed = usage["input_tokens"] < REPORTED_INPUT_TOKENS
        if changed:
            usage["input_tokens"] = REPORTED_INPUT_TOKENS
            usage["total_tokens"] = REPORTED_INPUT_TOKENS + usage["output_tokens"]
        # Commit before the client can observe the rewritten completion.
        self.record(upstream_usage=original, reported_usage=deepcopy(usage),
                    usage_override_applied=changed)
        return changed

    def _frame(self, frame):
        lines = frame.splitlines(keepends=True)
        data = [line[5:].lstrip(b" ").rstrip(b"\r\n") for line in lines if line.startswith(b"data:")]
        if not data:
            return frame
        try:
            value = json.loads(b"\n".join(data))
        except (ValueError, UnicodeError):
            return frame
        if not self._rewrite(value, event=True):
            return frame
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        result, inserted = [], False
        for line in lines:
            if not line.startswith(b"data:"):
                result.append(line)
            elif not inserted:
                ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                result.append(b"data: " + encoded + ending)
                inserted = True
        return b"".join(result)

    def chunks(self):
        if not self.enabled:
            yield from raw_chunks(self.response)
            return
        pending = bytearray()
        chunks = raw_chunks(self.response)
        if not self.mode:
            # Some compatible upstreams omit Content-Type even for native SSE.
            prefix = bytearray()
            for chunk in chunks:
                prefix.extend(chunk)
                stripped = bytes(prefix).removeprefix(b"\xef\xbb\xbf").lstrip()
                if stripped or len(prefix) >= 4096:
                    break
            self.mode = "application/json" if prefix.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"{") else "text/event-stream"
            chunks = chain((bytes(prefix),), chunks)
        if self.mode == "application/json":
            for chunk in chunks:
                pending.extend(chunk)
                if len(pending) > MAX_EVENT_BYTES:
                    yield bytes(pending)
                    yield from chunks
                    return
            try:
                value = json.loads(pending)
            except (ValueError, UnicodeError):
                yield bytes(pending)
                return
            if self._rewrite(value):
                yield json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")
            else:
                yield bytes(pending)
            return
        passthrough = False
        for chunk in chunks:
            pending.extend(chunk)
            while match := BOUNDARY.search(pending):
                frame, separator = bytes(pending[:match.start()]), bytes(pending[match.start():match.end()])
                del pending[:match.end()]
                yield (frame if passthrough or len(frame) > MAX_EVENT_BYTES else self._frame(frame)) + separator
                passthrough = False
            if len(pending) > MAX_EVENT_BYTES:
                # Keep enough suffix to recognize a delimiter crossing reads.
                yield bytes(pending[:-3])
                del pending[:-3]
                passthrough = True
        if pending:
            yield bytes(pending) if passthrough else self._frame(bytes(pending))
