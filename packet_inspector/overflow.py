"""Opt-in, per-chat overflow fallback. Never edits Codex history or replays a turn.

The Codex backend used by the desktop supports checkpoint summarization through
Responses, not the public /responses/compact endpoint. Like native compaction,
this adapter requests a text handoff without exposing executable tools.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from .analyzer import JsonBytes, MAX_BODY, decode_body, encode, thread_identity
from .transport import prepare_request
from .maintenance import SQLiteReclaimer
from .locking import file_lock


CHECKPOINT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
"""
# Match codex-prompts/templates/compact/{prompt,summary_prefix}.md and the
# user/input_text summary shape in core/src/compact.rs. This is still a relay
# prefix replacement, not a native session/world-state transition.
CHECKPOINT_PREFIX = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"
RETAINED_USER_TOKENS = 20_000
REPLACEMENT_FORMAT = 2
CALL_TYPES = {"function_call", "custom_tool_call", "computer_call", "local_shell_call", "tool_search_call"}
OUTPUT_TYPES = {kind + "_output" for kind in CALL_TYPES}
CATALOG_TYPES = {"additional_tools", "tool_definition"}
MAX_COMPACTIONS = 8
MAX_SUMMARY_BYTES = 2 * 1024 * 1024


class OverflowFailure(Exception):
    def __init__(self, code, message, status=413):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf8")).hexdigest()


def user_text_parts(item):
    """Plain user text can follow native retention without dropping annotations."""
    if item.get("role") != "user" or item.get("type", "message") != "message":
        return None
    if item.get("internal_chat_message_metadata_passthrough"):
        return None
    content = item.get("content")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "input_text"
            and isinstance(p.get("text"), str) and not (p.keys() - {"type", "text"}) for p in content):
        return content
    return None


def protected(item):
    if item.get("role") in {"system", "developer"} or item.get("type") in CATALOG_TYPES | {"compaction", "context_compaction"}:
        return True
    if item.get("role") != "user":
        return False
    parts = user_text_parts(item)
    if parts is None:
        # The relay lacks native annotation/media preparation and world state.
        # Keep these original objects rather than inventing their replacement.
        return True
    return any(p["text"].lstrip().startswith(("# AGENTS.md instructions", "<environment_context>")) for p in parts)


def truncate_user_parts(parts, tokens):
    """Port native UTF-8 head/tail retention, keeping the original part order."""
    raw = "".join(p["text"] for p in parts).encode("utf8")
    limit = tokens * 4  # codex-utils-string::approx_bytes_for_tokens
    if len(raw) <= limit:
        return parts
    left, right = limit // 2, limit - limit // 2
    start = len(raw[:left].decode("utf8", "ignore").encode("utf8"))
    end = len(raw) - (len(raw[-right:].decode("utf8", "ignore").encode("utf8")) if right else 0)
    marker = f"…{(len(raw) - limit + 3) // 4} tokens truncated…"
    result, offset, inserted = [], 0, False
    for part in parts:
        data = part["text"].encode("utf8")
        begin, offset = offset, offset + len(data)
        if offset <= start or begin >= end:
            result.append(part)
            continue
        prefix_end, suffix_start = max(0, start - begin), min(len(data), max(0, end - begin))
        if prefix_end:
            result.append({"type": "input_text", "text": data[:prefix_end].decode("utf8")})
        if not inserted:
            result.append({"type": "input_text", "text": marker})
            inserted = True
        if suffix_start < len(data):
            result.append({"type": "input_text", "text": data[suffix_start:].decode("utf8")})
    return result


def rebuild_prefix(items, summary, max_user_tokens=RETAINED_USER_TOKENS):
    """Native text retention + user summary; keep richer context conservatively."""
    selected = {i: item for i, item in enumerate(items) if protected(item)}
    remaining = max_user_tokens
    for index in range(len(items) - 1, -1, -1):
        if not remaining:
            break
        item = items[index]
        if index in selected:
            continue
        parts = user_text_parts(item)
        if parts is None:
            continue
        text = "".join(p["text"] for p in parts)
        if text.startswith(CHECKPOINT_PREFIX + "\n"):
            continue  # A previous summary is not a fresh user instruction.
        tokens = (len(text.encode("utf8")) + 3) // 4
        selected[index] = item if tokens <= remaining else {**item, "content": truncate_user_parts(parts, remaining)}
        remaining = max(0, remaining - tokens)
    history = [selected[i] for i in sorted(selected)]
    history.append({"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": CHECKPOINT_PREFIX + "\n" + summary}]})
    return history


def safe_cuts(items):
    """Only compact closed tool groups; always leave the last item/group intact."""
    pending = Counter()
    cuts = []
    for index, item in enumerate(items):
        kind = item.get("type", "message")
        if kind in CALL_TYPES | OUTPUT_TYPES:
            call_id = item.get("call_id") or item.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise OverflowFailure("overflow_tool_pairing", "工具调用缺少标识，无法安全切分压缩上下文。")
            if kind in CALL_TYPES:
                pending[call_id] += 1
            else:
                if not pending[call_id]:
                    raise OverflowFailure("overflow_tool_pairing", "发现没有对应调用的工具结果，未修改该请求。")
                pending[call_id] -= 1
        if not any(pending.values()) and index + 1 < len(items) and kind != "reasoning":
            cuts.append(index + 1)
    return cuts


def encode_changed(decoded, headers, path, compression):
    original_encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity").strip().lower()
    if original_encoding == "gzip":
        return gzip.compress(decoded, mtime=0), "gzip"
    if original_encoding == "zstd":
        import zstandard
        return zstandard.ZstdCompressor(level=3).compress(decoded), "zstd"
    return prepare_request(decoded, {}, "POST", path, compression)


class CheckpointStream:
    """A bounded SSE consumer; a 200 without response.completed is a failure."""
    def __init__(self):
        self.pending = b""
        self.received = 0
        self.completed = None

    def feed(self, chunk):
        self.received += len(chunk)
        if self.received > MAX_SUMMARY_BYTES:
            raise OverflowFailure("overflow_compaction_response_limit", "压缩响应超过本地大小限制。", 502)
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self._line(line.rstrip(b"\r"))

    def _line(self, line):
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            return
        try:
            event = json.loads(data)
        except (ValueError, UnicodeError):
            raise OverflowFailure("overflow_compaction_invalid_response", "压缩响应不是有效的 Responses 事件。", 502) from None
        kind = event.get("type") if isinstance(event, dict) else None
        if kind in {"error", "response.failed", "response.incomplete"}:
            raise OverflowFailure("overflow_compaction_failed", "上游未完成上下文压缩；原请求未发送。", 502)
        if kind == "response.completed":
            self.completed = event.get("response")

    def finish(self):
        if self.pending:
            self._line(self.pending.rstrip(b"\r"))
        value = self.completed
        if not isinstance(value, dict) or value.get("status") != "completed" or not isinstance(value.get("output"), list):
            raise OverflowFailure("overflow_compaction_incomplete", "上游没有返回完整压缩结果；原请求未发送。", 502)
        summary = None
        for item in value["output"]:
            if not isinstance(item, dict):
                raise OverflowFailure("overflow_compaction_nontext", "压缩摘要格式无效。", 502)
            kind = item.get("type")
            if kind == "reasoning":
                continue
            if kind not in {"message", "agent_message"} or item.get("role", "assistant") != "assistant":
                raise OverflowFailure("overflow_compaction_nontext", "压缩返回了非摘要内容；未执行工具或发送原请求。", 502)
            text = []  # Native compaction uses the last nonempty assistant message.
            content = item.get("content")
            if isinstance(content, str):
                text.append(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise OverflowFailure("overflow_compaction_nontext", "压缩返回了非文本内容。", 502)
                    text.append(part["text"])
            else:
                raise OverflowFailure("overflow_compaction_nontext", "压缩摘要格式无效。", 502)
            candidate = "".join(text)
            if candidate.strip():
                summary = candidate
        if summary is None:
            raise OverflowFailure("overflow_compaction_empty", "上游压缩摘要为空；原请求未发送。", 502)
        return summary


class OverflowCompactor:
    def __init__(self, directory: Path, *, ttl_seconds=7 * 86400, max_entries=256,
                 max_cache_bytes=64 * 1024 * 1024, cleanup_interval=600):
        self.path = Path(directory) / "overflow-context.sqlite"
        self.locks = [threading.Lock() for _ in range(64)]
        self.db_lock = threading.Lock()
        self.activity_lock = threading.Lock()
        self.active_keys, self.request_count = Counter(), 0
        self.busy = threading.Event()
        self.ttl_seconds, self.max_entries = ttl_seconds, max_entries
        self.max_cache_bytes, self.cleanup_interval = max_cache_bytes, cleanup_interval
        self.next_cleanup = 0.0
        self.last_cleanup = {"expired": 0, "evicted": 0, "entries": 0, "bytes": 0}
        self.reclaimer = SQLiteReclaimer(self.path, self.db_lock, interval=cleanup_interval)

    @contextmanager
    def request_scope(self):
        keys = set()
        with self.activity_lock:
            self.request_count += 1
            self.busy.set()
        try:
            yield keys
        finally:
            with self.activity_lock:
                for key in keys:
                    self.active_keys[key] -= 1
                    if not self.active_keys[key]:
                        del self.active_keys[key]
                self.request_count -= 1
                if not self.request_count:
                    self.busy.clear()

    def _protect(self, key, scope):
        with self.activity_lock:
            if key not in scope:
                scope.add(key)
                self.active_keys[key] += 1

    def _prune(self, db):
        # Caller holds db_lock. Keep the activity lock through commit so a
        # newly-starting request cannot race eviction after registering its key.
        with self.activity_lock:
            keys = tuple(self.active_keys)
            if len(keys) > 800:  # Stay below conservative SQLite parameter limits.
                self.next_cleanup = 0.0
                return
            exclude = " AND key NOT IN (" + ",".join("?" for _ in keys) + ")" if keys else ""
            expired = db.execute("SELECT key FROM mappings WHERE updated<?" + exclude +
                                 " ORDER BY updated,key LIMIT 256", (time.time() - self.ttl_seconds, *keys)).fetchall()
            db.executemany("DELETE FROM mappings WHERE key=?", expired)
            count, total = db.execute("SELECT count(*),coalesce(sum(length(output)),0) FROM mappings").fetchone()
            evicted = 0
            while (count > self.max_entries or total > self.max_cache_bytes) and evicted < 256:
                row = db.execute("SELECT key,length(output) FROM mappings WHERE 1" + exclude +
                                 " ORDER BY updated,key LIMIT 1", keys).fetchone()
                if row is None:
                    break  # Active mappings may exceed the cap until requests finish.
                db.execute("DELETE FROM mappings WHERE key=?", (row[0],))
                count, total, evicted = count - 1, total - row[1], evicted + 1
            db.commit()
            self.last_cleanup = {"expired": len(expired), "evicted": evicted, "entries": count, "bytes": total}
            self.next_cleanup = time.monotonic() + (5 if len(expired) == 256 or count > self.max_entries or
                                                    total > self.max_cache_bytes else self.cleanup_interval)
            if expired or evicted:
                self.reclaimer.request()

    def maintenance(self, *, force=False):
        if self.path.exists() and (force or time.monotonic() >= self.next_cleanup):
            if self.db_lock.acquire(blocking=False):
                try:
                    with self._connect() as db:
                        self._prune(db)
                finally:
                    self.db_lock.release()
        self.reclaimer.run(self.busy, force=force)
        return {**self.last_cleanup, "disk": dict(self.reclaimer.last)}

    def cache_status(self):
        return {"ttl_seconds": self.ttl_seconds, "max_entries": self.max_entries,
                "max_bytes": self.max_cache_bytes, "cleanup_interval_seconds": self.cleanup_interval,
                **self.last_cleanup, "disk": dict(self.reclaimer.last)}

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("CREATE TABLE IF NOT EXISTS mappings (key TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                           "covered INTEGER NOT NULL, fingerprint TEXT NOT NULL, output BLOB NOT NULL, updated REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS mappings_updated ON mappings(updated,key)")
                yield db
        finally:
            db.close()

    def _read(self, key):
        if not self.path.exists():
            return None
        with self.db_lock, self._connect() as db:
            row = db.execute("SELECT scope,covered,fingerprint,output,updated FROM mappings WHERE key=?", (key,)).fetchone()
            if row and row[4] < time.time() - self.ttl_seconds:
                db.execute("DELETE FROM mappings WHERE key=?", (key,))
                self.reclaimer.request()
                self.next_cleanup = 0.0
                return None
            if row:
                db.execute("UPDATE mappings SET updated=? WHERE key=?", (time.time(), key))
        if not row:
            return None
        try:
            output = json.loads(row[3])
            if row[1] <= 0 or not isinstance(output, list) or not output or not all(isinstance(i, dict) for i in output):
                raise ValueError("invalid mapping")
            return {"scope": row[0], "covered": row[1], "fingerprint": row[2], "output": output}
        except (ValueError, TypeError):
            self._clear(key)
            return None

    def _clear(self, key):
        if self.path.exists():
            with self.db_lock, self._connect() as db:
                removed = db.execute("DELETE FROM mappings WHERE key=?", (key,)).rowcount
                if removed:
                    self.reclaimer.request()
                    self.next_cleanup = 0.0

    def _save(self, key, scope, covered, items, output):
        with self.db_lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?,?)",
                       (key, scope, covered, digest(items[:covered]), encode(output), time.time()))
            self._prune(db)
            self.reclaimer.request()

    def prepare(self, original, original_encoding, body, headers, path, upstream,
                compression, budget, compact, *, request_scope=None):
        if request_scope is None:
            with self.request_scope() as scope:
                return self._prepare(original, original_encoding, body, headers, path, upstream,
                                     compression, budget, compact, scope)
        return self._prepare(original, original_encoding, body, headers, path, upstream,
                             compression, budget, compact, request_scope)

    def _prepare(self, original, original_encoding, body, headers, path, upstream,
                 compression, budget, compact, scope):
        """Return wire bytes/encoding/metrics; compact is an injected HTTP adapter.

        The original turn has NOT been sent. No inference retries occur here.
        A mapping is committed only after all compactions fit the final budget.
        """
        metrics = {"overflow_original_wire_bytes": len(original), "overflow_compaction_calls": 0,
                   "overflow_cache_hit": False, "overflow_cache_reset": False}
        try:
            decoded = decode_body(body, next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity"))
            value = json.loads(decoded)
            thread = thread_identity(headers, value)
        except Exception:
            if len(original) <= budget:
                return original, original_encoding, metrics
            raise OverflowFailure("overflow_invalid_json", "超限请求无法解析为 JSON；未发送上游。") from None
        if not thread:
            if len(original) <= budget:
                return original, original_encoding, metrics
            raise OverflowFailure("overflow_unknown_thread", "超限请求没有可识别的会话标识，未建立上下文替换。")
        lower = {k.lower(): v for k, v in headers.items()}
        auth_scope = {k: lower[k] for k in ("authorization", "chatgpt-account-id", "openai-organization", "openai-project", "x-api-key", "api-key") if k in lower}
        key = digest([upstream, path, thread, auth_scope])
        self._protect(key, scope)
        lock = self.locks[int(key[:8], 16) % len(self.locks)]
        with lock, file_lock(self.path.parent / "overflow-locks" / (str(int(key[:8], 16) % len(self.locks)) + ".lock")):
            if len(original) <= budget:
                existed = self._read(key) is not None
                self._clear(key)
                metrics["overflow_cache_reset"] = existed
                return original, original_encoding, metrics
            if not isinstance(value, dict) or not isinstance(value.get("input"), list) or not value["input"] or not all(isinstance(i, dict) for i in value["input"]):
                raise OverflowFailure("overflow_unsupported_input", "超限兜底仅支持 Responses 的完整 input 数组。")
            if value.get("previous_response_id") or value.get("conversation"):
                raise OverflowFailure("overflow_stateful_input", "超限兜底不改写服务端会话引用。")
            items = value["input"]
            scope = digest({"model": value.get("model"), "instructions": value.get("instructions"),
                            "tools": value.get("tools"), "replacement_format": REPLACEMENT_FORMAT})
            mapping = self._read(key)
            covered, replacement = 0, []
            if mapping:
                count = mapping["covered"]
                if mapping["scope"] == scope and count <= len(items) and digest(items[:count]) == mapping["fingerprint"]:
                    covered, replacement = count, mapping["output"]
                    metrics["overflow_cache_hit"] = True
                else:
                    self._clear(key)
                    metrics["overflow_cache_reset"] = True
            current = replacement + items[covered:]
            span = JsonBytes(decoded).spans["/input"]

            def wire(candidate):
                changed = decoded[:span[0]] + encode(candidate) + decoded[span[1]:]
                if len(changed) > MAX_BODY:
                    raise OverflowFailure("overflow_body_limit", "替换后的上下文超过本地解析限制。")
                return encode_changed(changed, headers, path, compression)

            result, encoding = wire(current) if mapping and metrics["overflow_cache_hit"] else (original, original_encoding)
            calls = 0
            started = time.monotonic()
            while len(result) > budget:
                if calls >= MAX_COMPACTIONS or time.monotonic() - started > 240:
                    raise OverflowFailure("overflow_compaction_limit", "本次超限兜底达到压缩次数或时间限制；原请求未发送。")
                cuts = [c for c in safe_cuts(current) if c > len(replacement) and any(not protected(i) for i in current[:c])]
                if not cuts:
                    raise OverflowFailure("overflow_no_safe_prefix", "没有可安全压缩的旧上下文；最新内容或工具定义可能过大。")
                # Select a prefix whose *own* encoded compaction request fits.
                lo, hi, plan = 0, len(cuts) - 1, None
                while lo <= hi:
                    mid = (lo + hi) // 2
                    cut = cuts[mid]
                    request_body = self._checkpoint_request(value, current[:cut], key)
                    request_wire, request_encoding = encode_changed(encode(request_body), headers, path, compression)
                    if len(request_wire) <= budget:
                        plan = (cut, request_wire, request_encoding)
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if plan is None:
                    raise OverflowFailure("overflow_compaction_too_large", "最小安全压缩请求仍超过上传限制；原请求未发送。")
                cut, request_wire, request_encoding = plan
                summary = compact(request_wire, request_encoding)
                if not isinstance(summary, str) or not summary.strip():
                    raise OverflowFailure("overflow_compaction_empty", "上游压缩摘要为空；原请求未发送。", 502)
                next_replacement = rebuild_prefix(current[:cut], summary)
                candidate = next_replacement + current[cut:]
                candidate_wire, candidate_encoding = wire(candidate)
                if len(candidate_wire) >= len(result):
                    raise OverflowFailure("overflow_compaction_no_progress", "压缩未缩小上传包；原请求未发送。")
                covered += cut - len(replacement)
                replacement = next_replacement
                current, result, encoding = candidate, candidate_wire, candidate_encoding
                calls += 1
                metrics["overflow_compaction_calls"] = calls
                metrics["overflow_compaction_max_wire_bytes"] = max(metrics.get("overflow_compaction_max_wire_bytes", 0), len(request_wire))
            if calls:
                self._save(key, scope, covered, items, replacement)
            metrics.update(overflow_covered_items=covered, overflow_final_wire_bytes=len(result),
                           overflow_compaction_ms=round((time.monotonic() - started) * 1000, 1))
            return result, encoding, metrics

    @staticmethod
    def _checkpoint_request(original, prefix, key):
        # Follow native protocol additions by inheriting the whole request.
        # Change only fields tied to the summary's input, output or execution.
        value = deepcopy(original)
        for name in ("tools", "previous_response_id", "conversation", "max_output_tokens"):
            value.pop(name, None)
        # Catalogs would expose executable tools during summarization. Keep them
        # unchanged in the replacement, but don't advertise them in this call.
        value.update({"stream": True, "store": False,
                 "input": deepcopy([i for i in prefix if i.get("type") not in CATALOG_TYPES]) + [
                     {"role": "user", "content": [{"type": "input_text", "text": CHECKPOINT_PROMPT}]}],
                 "tool_choice": "auto", "parallel_tool_calls": False,
                 "prompt_cache_key": "overflow-" + key[:32]})
        if "background" in value:
            value["background"] = False
        if isinstance(value.get("text"), dict):
            value["text"].pop("format", None)
        metadata = value.get("client_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            turn = json.loads(metadata.get("x-codex-turn-metadata", "{}"))
        except (TypeError, ValueError):
            turn = {}
        turn = turn if isinstance(turn, dict) else {}
        compaction = turn.get("compaction")
        compaction = compaction if isinstance(compaction, dict) else {}
        compaction.update(trigger="upload_overflow", implementation="responses")
        turn.update(request_kind="compaction", compaction=compaction)
        metadata["x-codex-turn-metadata"] = json.dumps(turn)
        value["client_metadata"] = metadata
        return value
