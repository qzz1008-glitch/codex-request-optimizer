"""Opt-in Responses WebSocket transport, with lossless, non-generating rehydration.

Codex still supplies the complete request. Only hashes and a response ID survive
between requests; neither conversation contents nor credentials are persisted.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from email.message import Message
import hashlib
import io
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import proxy_bypass

from .analyzer import MAX_BODY, decode_body, thread_identity
from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .overflow import OverflowFailure, safe_cuts


MAX_SESSIONS = 64
IDLE_SECONDS = 300
# Reopen before the documented 60-minute connection limit, between requests.
CONNECTION_SECONDS = 55 * 60
TERMINAL = {"response.completed", "response.failed", "response.incomplete"}


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def item_hash(item):
    # Codex may drop server bookkeeping on replay. Never ignore opaque context,
    # tool call IDs, channels, summary contents or future/unknown item fields.
    item = dict(item)
    kind = item.get("type", "message")
    if kind == "message":
        item.setdefault("type", "message")
    if kind in {"message", "reasoning"} or (kind in {"function_call", "custom_tool_call"} and item.get("call_id")):
        item.pop("id", None)
        if item.get("status") == "completed":
            item.pop("status")
        # Match native continuation comparisons for message/call metadata.
        # Result metadata stays significant: late tool results may update it.
        item.pop("internal_chat_message_metadata_passthrough", None)
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).digest()


class IncrementalFailure(OSError):
    def __init__(self, code, message, status=502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class CacheMiss(Exception):
    pass


def is_cache_miss(event):
    error = event.get("error")
    if not isinstance(error, dict) and isinstance(event.get("response"), dict):
        error = event["response"].get("error")
    if not isinstance(error, dict):
        return False
    if error.get("code") in {"previous_response_not_found", "websocket_connection_limit_reached"}:
        return True
    # The Codex backend also returns an unstructured parameter-specific error.
    message = str(error.get("message", "")).lower()
    return ("previous_response_id" in message and
            ("invalid" in message or "not found" in message or "expired" in message))


@dataclass
class Plan:
    endpoint: str
    key: bytes
    body: dict
    hashes: list
    parameters: bytes
    budget: int

    def frame(self, items, previous=None, *, warmup=False):
        value = {**self.body, "type": "response.create", "input": items}
        value.pop("stream", None)
        value.pop("background", None)
        if previous:
            value["previous_response_id"] = previous
        if warmup:
            value["generate"] = False
        return serialize(value)


def prepare(endpoint, body, encoding, headers, session_id, budget, *, scope=""):
    """Return None for protocols whose semantics this adapter cannot preserve."""
    target = urlsplit(endpoint)
    if (target.scheme != "https" or target.hostname not in {"chatgpt.com", "api.openai.com"}
            or not target.path.rstrip("/").endswith("/responses")):
        return None
    try:
        value = json.loads(decode_body(body, encoding))
    except (ValueError, TypeError):
        return None
    if (not isinstance(value, dict) or value.get("stream") is not True
            or value.get("store") is not False or not isinstance(value.get("input"), list)
            or not value["input"] or not all(isinstance(item, dict) for item in value["input"])
            or value.get("previous_response_id") or value.get("conversation")
            or value.get("background") or "generate" in value
            or value.get("context_management") or value.get("truncation") not in (None, "disabled")):
        return None
    normalized = {key.lower(): val for key, val in headers.items()}
    auth = normalized.get("authorization", "")
    session_id = session_id or thread_identity(headers, value)
    if not auth or not session_id:
        return None
    binding = serialize([endpoint, session_id, auth, normalized.get("chatgpt-account-id", ""),
                         normalized.get("openai-organization", ""), normalized.get("openai-project", ""), scope])
    # Native Codex also excludes per-turn telemetry and delivery options from
    # its continuation comparison; forward their current values on every frame.
    parameters = hashlib.sha256(json.dumps({key: val for key, val in value.items()
                                           if key not in {"input", "client_metadata", "stream_options"}},
                                           ensure_ascii=False, sort_keys=True).encode()).digest()
    return Plan(endpoint, hashlib.sha256(binding).digest(), value,
                [item_hash(item) for item in value["input"]], parameters, budget)


@dataclass
class Session:
    guard: threading.Lock = field(default_factory=threading.Lock)
    socket: object = None
    response_id: str = ""
    hashes: list = field(default_factory=list)
    parameters: bytes = b""
    connected: float = 0
    touched: float = 0

    def reset(self):
        socket, self.socket = self.socket, None
        self.response_id, self.hashes, self.parameters = "", [], b""
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass


def connect_socket(endpoint, headers, proxies, context, timeout):
    from websockets.sync.client import connect
    target = urlsplit(endpoint)
    uri = urlunsplit(("wss", target.netloc, target.path, target.query, ""))
    omitted = {"host", "connection", "upgrade", "content-length", "content-encoding",
               "content-type", "accept", "accept-encoding", "user-agent", "proxy-authorization"}
    outgoing = {key: value for key, value in headers.items()
                if key.lower() not in omitted and not key.lower().startswith("sec-websocket-")}
    proxy = None if proxy_bypass(target.netloc) else proxies.get("https") or proxies.get("http") or None
    return connect(uri, additional_headers=outgoing, ssl=context,
                   proxy=proxy,
                   proxy_ssl=context if urlsplit(proxy or "").scheme == "https" else None,
                   user_agent_header=next((value for key, value in headers.items() if key.lower() == "user-agent"), None),
                   compression=None, open_timeout=min(15, timeout), close_timeout=2,
                   ping_interval=20, ping_timeout=20, max_size=MAX_BODY)


class BufferedResponse(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(body)
        self.status, self.reason = status, "Upstream WebSocket error"
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        if headers:
            if headers.get("content-type"):
                self.headers.replace_header("Content-Type", headers["content-type"])
            for key in ("x-request-id", "x-codex-turn-state", "retry-after"):
                if headers.get(key):
                    self.headers[key] = headers[key]


class IncrementalTransport:
    def __init__(self, *, connector=connect_socket, clock=time.monotonic, max_sessions=MAX_SESSIONS):
        self.connector, self.clock, self.max_sessions = connector, clock, max_sessions
        self.guard = threading.Lock()
        self.sessions = OrderedDict()

    def clear(self):
        # Active responses finish on their existing connection. A detached active
        # session is closed by its response lease, never by a settings/UI thread.
        with self.guard:
            sessions, self.sessions = list(self.sessions.values()), OrderedDict()
            for session in sessions:
                if session.guard.acquire(blocking=False):
                    try:
                        session.reset()
                    finally:
                        session.guard.release()

    def prune(self):
        with self.guard:
            now = self.clock()
            for key, session in list(self.sessions.items()):
                if now - session.touched >= IDLE_SECONDS and session.guard.acquire(blocking=False):
                    try:
                        session.reset()
                        del self.sessions[key]
                    finally:
                        session.guard.release()

    def _lease(self, key, timeout):
        with self.guard:
            now = self.clock()
            for old_key, session in list(self.sessions.items()):
                expired = now - session.touched >= IDLE_SECONDS
                crowded = key not in self.sessions and len(self.sessions) >= self.max_sessions
                if (expired or crowded) and session.guard.acquire(blocking=False):
                    try:
                        session.reset()
                        del self.sessions[old_key]
                    finally:
                        session.guard.release()
            session = self.sessions.get(key)
            if session is None:
                if len(self.sessions) >= self.max_sessions:
                    raise IncrementalFailure("incremental_busy", "增量传输连接正忙，请稍后重试。", 503)
                session = self.sessions[key] = Session(touched=now)
            self.sessions.move_to_end(key)
        if not session.guard.acquire(timeout=max(.001, timeout)):
            raise IncrementalFailure("incremental_busy", "同一会话的上一条增量请求尚未结束。", 503)
        return session

    def _release(self, plan, session, *, valid):
        with self.guard:
            if not valid or self.sessions.get(plan.key) is not session:
                session.reset()
            session.touched = self.clock()
            session.guard.release()

    @staticmethod
    def _recv(session, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Incremental context preparation timed out")
        data = session.socket.recv(timeout=remaining)
        if not isinstance(data, str):
            raise IncrementalFailure("incremental_event_invalid", "上游返回了无法识别的 WebSocket 事件。")
        event = json.loads(data)
        if not isinstance(event, dict):
            raise IncrementalFailure("incremental_event_invalid", "上游返回了无法识别的 WebSocket 事件。")
        return event

    @staticmethod
    def _send(session, frame, meta):
        session.socket.send(frame.decode("utf-8"))
        meta["incremental_max_frame_bytes"] = max(meta["incremental_max_frame_bytes"], len(frame))

    def _warmup(self, session, plan, items, cuts, meta, deadline):
        start = 0
        sizes = [0]
        for item in items:
            sizes.append(sizes[-1] + len(serialize(item)))
        while start < len(items):
            # Leave headroom below the configured protection threshold. A single
            # atomic group may use the full budget, but is never cut internally.
            target = plan.budget - min(50_001, plan.budget // 10)
            stop = None
            overhead = len(plan.frame([], session.response_id, warmup=True))
            if overhead > plan.budget:
                raise IncrementalFailure("incremental_parameters_too_large", "请求参数超过上传上限，无法分段。", 413)
            for cut in cuts:
                if cut <= start:
                    continue
                length = overhead + sizes[cut] - sizes[start] + cut - start - 1
                if length > plan.budget:
                    break
                if length > target and stop is not None:
                    break
                stop = cut
                if length >= target:
                    break
            if stop is None:
                raise IncrementalFailure("incremental_item_too_large", "单个上下文条目或完整工具组超过上传上限，无法无损分段。", 413)
            frame = plan.frame(items[start:stop], session.response_id, warmup=True)
            self._send(session, frame, meta)
            meta["incremental_bootstrap_requests"] += 1
            meta["incremental_bootstrap_bytes"] += len(frame)
            while True:
                event = self._recv(session, deadline)
                kind = event.get("type")
                if is_cache_miss(event):
                    raise CacheMiss()
                if kind == "error":
                    raise IncrementalFailure("incremental_restore_failed", str((event.get("error") or {}).get("message", "上游拒绝恢复上下文。")))
                if kind in TERMINAL:
                    response = event.get("response") or {}
                    if (kind != "response.completed" or response.get("status") != "completed"
                            or response.get("output") or not response.get("id")):
                        raise IncrementalFailure("incremental_restore_failed", "上游未按无生成模式完成上下文恢复，当前问题未发送。")
                    session.response_id = response["id"]
                    start = stop
                    break

    def open(self, plan, headers, proxies, context, record, *, timeout=UPSTREAM_READ_TIMEOUT_SECONDS,
             retain=lambda: True):
        deadline = time.monotonic() + timeout
        session = self._lease(plan.key, timeout)
        meta = {"incremental_transport": "websocket", "incremental_reused_items": 0,
                "incremental_bootstrap_requests": 0, "incremental_bootstrap_bytes": 0,
                "incremental_recoveries": 0, "incremental_max_frame_bytes": 0}
        try:
            for recovery in range(2):
                state = getattr(getattr(session.socket, "state", None), "name", "OPEN")
                if session.socket is not None and state != "OPEN":
                    session.reset()
                    meta["incremental_reset_reason"] = "connection_closed"
                if session.socket is not None and self.clock() - session.connected >= CONNECTION_SECONDS:
                    session.reset()
                    meta["incremental_reset_reason"] = "connection_age"
                count = len(session.hashes)
                reusable = (session.socket is not None and bool(session.response_id) and count > 0
                            and session.parameters == plan.parameters and plan.hashes[:count] == session.hashes)
                if not reusable and session.socket is not None:
                    session.reset()
                    meta.setdefault("incremental_reset_reason", "context_changed")
                if session.socket is None:
                    session.socket = self.connector(plan.endpoint, headers, proxies, context,
                                                    max(.001, deadline - time.monotonic()))
                    session.connected = self.clock()
                meta["incremental_reused_items"] = count if reusable else 0
                pending = plan.body["input"][count:] if reusable else plan.body["input"]
                frame = plan.frame(pending, session.response_id)
                try:
                    if len(frame) > plan.budget:
                        # Keep the last item/complete tool group on the real
                        # generation request. Native compaction triggers and
                        # current user/tool inputs must not execute in warmups.
                        offset = count if reusable else 0
                        try:
                            cuts = [cut - offset for cut in safe_cuts(plan.body["input"]) if cut > offset]
                        except (OverflowFailure, KeyError, TypeError) as exc:
                            raise IncrementalFailure("incremental_context_pairing", "上下文的工具配对不完整，未拆分或修改该请求。", 422) from exc
                        if not cuts or len(plan.frame(pending[cuts[-1]:])) > plan.budget:
                            raise IncrementalFailure("incremental_item_too_large", "单个上下文条目或完整工具组超过上传上限，无法无损分段。", 413)
                        stop = cuts[-1]
                        self._warmup(session, plan, pending[:stop], cuts, meta, deadline)
                        frame = plan.frame(pending[stop:], session.response_id)
                    if len(frame) > plan.budget:
                        raise IncrementalFailure("incremental_parameters_too_large", "请求参数超过上传上限，无法分段。", 413)
                    self._send(session, frame, meta)
                    meta.update(outbound_wire_bytes=len(frame), outbound_content_encoding="websocket-json",
                                relay_wire_bytes=len(frame), relay_content_encoding="websocket-json",
                                relay_zstd_level=None, transport_content_encoding="websocket-json", transport_zstd_level=None)
                    prefix = bytearray()
                    while True:
                        event = self._recv(session, deadline)
                        if is_cache_miss(event):
                            raise CacheMiss()
                        if event.get("type") == "error":
                            status = event.get("status", 400)
                            response = BufferedResponse(status if type(status) is int else 400,
                                                        serialize({"error": event.get("error", {})}))
                            record(**meta)
                            self._release(plan, session, valid=False)
                            return response
                        prefix.extend(b"data: " + serialize(event) + b"\n\n")
                        if len(prefix) > MAX_BODY:
                            raise IncrementalFailure("incremental_event_invalid", "上游未及时开始响应。")
                        if event.get("type") in {"response.created", "response.in_progress"} | TERMINAL:
                            record(**meta)
                            return StreamResponse(self, session, plan, bytes(prefix), event, timeout, retain)
                except CacheMiss:
                    session.reset()
                    meta["incremental_recoveries"] += 1
                    meta["incremental_reset_reason"] = "cache_miss"
                    if recovery:
                        raise IncrementalFailure("incremental_cache_unavailable", "重建后上游上下文缓存仍不可用，已停止自动恢复。") from None
            raise AssertionError("unreachable")
        except Exception as exc:
            record(**meta)
            self._release(plan, session, valid=False)
            handshake = getattr(exc, "response", None)
            if handshake is not None and type(getattr(handshake, "status_code", None)) is int:
                return BufferedResponse(handshake.status_code, bytes(getattr(handshake, "body", b""))[:128 * 1024],
                                        getattr(handshake, "headers", {}))
            raise


class StreamResponse:
    """Small urllib-compatible SSE facade; owns one serialized socket lease."""
    status, reason = 200, "OK"

    def __init__(self, owner, session, plan, prefix, first, timeout, retain):
        self.owner, self.session, self.plan = owner, session, plan
        self.buffer, self.timeout = bytearray(prefix), timeout
        self.retain = retain
        self.closed, self.finished, self.valid = False, False, False
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        response_headers = getattr(getattr(session.socket, "response", None), "headers", {})
        for key in ("x-request-id", "x-codex-turn-state"):
            if response_headers.get(key):
                self.headers[key] = response_headers[key]
        self._terminal(first)

    def _terminal(self, event):
        kind = event.get("type")
        if kind not in TERMINAL | {"error"}:
            return
        self.finished = True
        response = event.get("response") or {}
        if kind == "response.completed" and response.get("status") == "completed" and response.get("id"):
            self.session.response_id = response["id"]
            self.session.hashes = self.plan.hashes + [item_hash(item) for item in response.get("output", [])]
            self.session.parameters = self.plan.parameters
            self.valid = True

    def read1(self, size=65536):
        if self.closed:
            return b""
        if not self.buffer and not self.finished:
            try:
                event = self.owner._recv(self.session, time.monotonic() + self.timeout)
                self.buffer.extend(b"data: " + serialize(event) + b"\n\n")
                self._terminal(event)
            except Exception:
                self.close()
                raise
        if size < 0:
            size = len(self.buffer)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def read(self, size=-1):
        if size >= 0:
            return self.read1(size)
        parts = []
        while chunk := self.read1():
            parts.append(chunk)
        return b"".join(parts)

    def close(self):
        if not self.closed:
            self.closed = True
            self.owner._release(self.plan, self.session, valid=self.valid and self.retain())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
