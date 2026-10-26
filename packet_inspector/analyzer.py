"""Byte accounting over the actual JSON, plus a separately redacted viewing copy."""
from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .request_context import is_delegation_notice, request_image_paths, request_session_context, temporary_request_kind

MAX_BODY = 128 * 1024 * 1024
CLASSIFICATION_VERSION = 2
SECRET_KEYS = {"authorization", "proxy-authorization", "cookie", "set-cookie",
               "x-api-key", "api-key", "api_key", "apikey", "access_token",
               "refresh_token", "id_token", "client_secret", "password",
               "api_token", "bearer_token", "experimental_bearer_token"}
SAFE_HEADERS = {"content-type", "content-encoding", "content-length", "accept",
                "accept-encoding", "user-agent", "originator", "session_id",
                "x-codex-thread-id", "x-codex-session-id", "x-request-id",
                "x-openai-request-id", "x-openai-conversation-id"}
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b|\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.I)
DATA_PATTERN = re.compile(r"data:(?:image|audio|video)/[^\s\"'<>]+", re.I)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def clean_text(text: str) -> str:
    text = KEY_PATTERN.sub("[credential redacted]", text)
    return DATA_PATTERN.sub(lambda m: f"[media data URI: {len(m[0].encode())} bytes, omitted]", text)


def redact(value, key="", counts=None):
    counts = counts if counts is not None else Counter()
    if key.lower() in SECRET_KEYS:
        counts["credentials"] += 1
        return "[credential redacted]"
    if isinstance(value, dict):
        return {k: redact(v, k, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, "", counts) for v in value]
    if isinstance(value, str):
        if key in {"image_url", "image_data", "b64_json", "audio_data"}:
            counts["media"] += 1
            return f"[media value: {len(value.encode())} bytes, omitted]"
        result = clean_text(value)
        counts["strings"] += result != value
        return result
    return value


def safe_headers(headers):
    # Unknown header values may be credentials. Names remain visible.
    return {k: clean_text(v) if k.lower() in SAFE_HEADERS else "[value not stored]"
            for k, v in headers.items()}


def safe_path(path):
    p = urlsplit(path)
    return urlunsplit(("", "", p.path, urlencode([(k, "[redacted]") for k, _ in parse_qsl(p.query)]), ""))


def classify_request(value, headers=None, path=""):
    """Classify this dispatch, without treating historical summaries as requests."""
    result = {"is_compaction": False, "classification_version": CLASSIFICATION_VERSION}
    if urlsplit(path).path.rstrip("/").endswith("/alpha/search"):
        return {**result, "request_kind": "tool_search"}
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    client = value.get("client_metadata", {}) if isinstance(value, dict) else {}
    client = client if isinstance(client, dict) else {}
    candidates = (lower.get("x-codex-turn-metadata"), client.get("x-codex-turn-metadata"), client)
    current = None
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                continue
        if isinstance(candidate, dict) and isinstance(candidate.get("request_kind"), str) and candidate["request_kind"] in {"compaction", "turn", "prewarm", "memory"}:
            current = candidate
            break
    if path.split("?", 1)[0].rstrip("/").endswith("/compact"):
        result.update(is_compaction=True, compaction_reason="compact_endpoint")
    elif current is not None:
        result["request_kind"] = current["request_kind"]
        if current["request_kind"] == "compaction":
            result.update(is_compaction=True, compaction_reason="request_metadata")
    else:
        inputs = value.get("input") if isinstance(value, dict) else None
        # Dispatch appends an active trigger; earlier items may be old history.
        if isinstance(inputs, list) and inputs and isinstance(inputs[-1], dict):
            if inputs[-1].get("type") in ("compaction_trigger", "context_compaction"):
                result.update(is_compaction=True, compaction_reason="input_trigger")
    if result["is_compaction"] and current is not None and isinstance(current.get("compaction"), dict):
        result["compaction_details"] = {
            k: clean_text(v)[:80] for k, v in current["compaction"].items()
            if k in {"trigger", "reason", "implementation", "phase", "strategy"} and isinstance(v, str)
        }
    return result


def decode_body(raw: bytes, encoding: str) -> bytes:
    import io
    encoding = encoding.strip().lower()
    if encoding in {"", "identity"}:
        if len(raw) > MAX_BODY:
            raise ValueError("inspection_limit")
        return raw
    if encoding == "gzip":
        reader = gzip.GzipFile(fileobj=io.BytesIO(raw))
    elif encoding == "zstd":
        import zstandard
        reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
    else:
        raise ValueError("unsupported_content_encoding")
    with reader:
        result = reader.read(MAX_BODY + 1)
    if len(result) > MAX_BODY:
        raise ValueError("inspection_limit")
    return result


def pointer(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def resolve(value, path):
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError("invalid_json_pointer")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class JsonBytes:
    """Index original value spans, without canonical reserialization estimates.

    Container/key punctuation is accounted for separately. The JSON has already
    passed json.loads. Bytes within quoted strings are never interpreted here.
    """
    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0
        self.sizes = {}
        self.spans = {}
        self._value("", 0)

    def _ws(self):
        while self.pos < len(self.raw) and self.raw[self.pos] in b" \r\n\t":
            self.pos += 1

    def _string(self):
        start = self.pos
        self.pos += 1
        while self.pos < len(self.raw):
            b = self.raw[self.pos]
            self.pos += 1
            if b == 92:
                self.pos += 1
            elif b == 34:
                return start, self.pos
        raise ValueError("unterminated_json_string")

    def _value(self, path, depth):
        if depth > 180:
            raise ValueError("json_depth_limit")
        self._ws()
        start = self.pos
        first = self.raw[self.pos]
        if first == 34:
            self._string()
        elif first in (123, 91):
            self.pos += 1
            self._ws()
            close = 125 if first == 123 else 93
            index = 0
            while self.raw[self.pos] != close:
                if first == 123:
                    a, b = self._string()
                    key = json.loads(self.raw[a:b])
                    self._ws()
                    self.pos += 1  # colon
                else:
                    key = index
                self._value(pointer(path, key), depth + 1)
                index += 1
                self._ws()
                if self.raw[self.pos] == 44:
                    self.pos += 1
                    self._ws()
                else:
                    break
            self.pos += 1
        else:
            while self.pos < len(self.raw) and self.raw[self.pos] not in b",]} \r\n\t":
                self.pos += 1
        self.sizes[path] = self.pos - start
        self.spans[path] = (start, self.pos)


def item_kind(item):
    if not isinstance(item, dict):
        return "other"
    kind = str(item.get("type") or ("message" if "role" in item else "other"))
    return kind + ":" + str(item.get("role", "unknown")) if kind == "message" else kind


def item_label(item):
    if not isinstance(item, dict):
        return str(item)[:100]
    if item.get("type") == "reasoning":
        return "推理密文" if item.get("encrypted_content") else "推理"
    label = item.get("name", "")
    if not label:
        content = item.get("content", item.get("output", item.get("arguments", "")))
        if isinstance(content, list):
            label = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        elif isinstance(content, str):
            label = content
    return clean_text(str(label))[:140]


def summarize(decoded: bytes, value, index: JsonBytes):
    top = []
    if isinstance(value, dict):
        top = [{"name": key, "bytes": index.sizes[pointer("", key)]} for key in value]
    accounted = sum(row["bytes"] for row in top)
    groups = Counter()
    items = []
    calls = Counter()
    outputs = Counter()
    encrypted = 0
    catalog_count = 0
    catalog_hashes = Counter()
    input_value = value.get("input") if isinstance(value, dict) else None
    for i, item in enumerate(input_value if isinstance(input_value, list) else []):
        if not isinstance(item, dict):
            continue
        path = pointer("/input", i)
        size = index.sizes[path]
        kind = item_kind(item)
        groups[kind] += size
        ciphertext = item.get("encrypted_content")
        if isinstance(ciphertext, str):
            encrypted += len(ciphertext.encode())
        if kind in {"additional_tools", "tool_definition"}:
            catalog_count += 1
            catalog_hashes[hashlib.sha256(encode(item.get("tools"))).hexdigest()] += 1
        if kind in {"function_call", "custom_tool_call"}:
            calls[item.get("call_id")] += 1
        if kind in {"function_call_output", "custom_tool_call_output"} and not is_delegation_notice(item):
            outputs[item.get("call_id")] += 1
        items.append({"index": i, "path": path, "kind": kind, "bytes": size,
                      "name": clean_text(str(item.get("name", ""))), "label": item_label(item),
                      "fingerprint": hashlib.sha256(encode(item)).hexdigest()})
    pairing = {"calls": sum(calls.values()), "outputs": sum(outputs.values()),
               "missing_outputs": sum((calls - outputs).values()),
               "orphan_outputs": sum((outputs - calls).values())}
    return {"decoded_bytes": len(decoded), "top_fields": top,
            "json_envelope_bytes": len(decoded) - accounted,
            "input_groups": dict(groups), "input_items": items, "pairing": pairing,
            "encrypted_content_bytes": encrypted, "catalog_count": catalog_count,
            "duplicate_catalogs": sum(n - 1 for n in catalog_hashes.values())}


def thread_identity(headers, value):
    lower = {k.lower(): v for k, v in headers.items()}
    for name in ("session_id", "x-codex-thread-id", "x-codex-session-id", "x-openai-conversation-id"):
        if lower.get(name):
            return clean_text(lower[name])[:160]
    for key in ("x-codex-turn-metadata", "x-codex-client-metadata"):
        try:
            meta = json.loads(lower.get(key, "{}"))
            for name in ("conversation_id", "conversation.id", "thread_id", "thread.id", "session_id"):
                if isinstance(meta, dict) and meta.get(name):
                    return clean_text(str(meta[name]))[:160]
        except ValueError:
            pass
    meta = value.get("metadata", {}) if isinstance(value, dict) else {}
    if isinstance(meta, dict):
        for name in ("thread_id", "conversation_id", "session_id"):
            if meta.get(name):
                return clean_text(str(meta[name]))[:160]
    return ""


def inspect_request(raw, headers, path=""):
    lowered = {k.lower(): v for k, v in headers.items()}
    meta = {"wire_bytes": len(raw), "content_encoding": lowered.get("content-encoding", "identity"),
            "headers": safe_headers(headers), "sha256": hashlib.sha256(raw).hexdigest(),
            **classify_request(None, headers, path)}
    if not raw:
        return {**meta, "decoded_bytes": 0, "thread_id": "", "inspection": "empty"}, None
    try:
        decoded = decode_body(raw, lowered.get("content-encoding", ""))
        value = json.loads(decoded)
        index = JsonBytes(decoded)
        stats = summarize(decoded, value, index)
        meta.update(classify_request(value, headers, path))
        counts = Counter()
        view = redact(value, counts=counts)
        thread = thread_identity(headers, value)
        model = value.get("model", "") if isinstance(value, dict) else ""
        meta.update(stats)
        meta["image_paths"] = request_image_paths(value)
        meta.update(request_session_context(value, headers))
        kind = temporary_request_kind(value)
        if kind:
            meta["request_kind"] = kind
        meta.update(thread_id=thread, model=clean_text(str(model))[:120],
                    inspection="json", redactions=dict(counts))
        return meta, {"body": view, "source_sizes": index.sizes}
    except Exception as exc:
        # Capture must not change relay behavior when an inspector cannot decode a body.
        return {**meta, "inspection": "unavailable", "inspection_error": type(exc).__name__,
                "thread_id": ""}, None


def describe_node(value, path, sizes, offset=0, limit=100):
    node = resolve(value, path)
    def brief(child, child_path, label):
        kind = "object" if isinstance(child, dict) else "array" if isinstance(child, list) else type(child).__name__
        count = len(child) if isinstance(child, (dict, list, str)) else None
        preview = child[:180] if isinstance(child, str) else child if not isinstance(child, (dict, list)) else None
        return {"path": child_path, "label": str(label), "kind": kind, "count": count,
                "bytes": sizes.get(child_path), "preview": preview}
    result = brief(node, path, "根节点" if not path else path.rsplit("/", 1)[-1])
    if isinstance(node, (dict, list)):
        pairs = list(node.items()) if isinstance(node, dict) else list(enumerate(node))
        result["children"] = [brief(v, pointer(path, k), k) for k, v in pairs[offset:offset + limit]]
        result["next_offset"] = offset + limit if offset + limit < len(pairs) else None
    return result


def search_body(value, needle, limit=60):
    needle = needle.casefold()
    found = []
    def visit(node, path):
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = pointer(path, key)
                if needle in key.casefold() and len(found) < limit:
                    found.append({"path": child_path, "preview": key})
                visit(child, child_path)
        elif isinstance(node, list):
            for i, child in enumerate(node):
                visit(child, pointer(path, i))
        elif isinstance(node, str):
            at = node.casefold().find(needle)
            if at >= 0:
                found.append({"path": path, "preview": node[max(0, at - 45):at + 150]})
    if needle:
        visit(value, "")
    return found


def compressed_packet_size(meta):
    """Transport-encoded request size before any summary replacement."""
    for key in ("overflow_original_wire_bytes", "usage_override_request_bytes", "outbound_wire_bytes"):
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def compare_packets(previous, current):
    old = previous.get("input_items", [])
    new = current.get("input_items", [])
    def unmatched(left, right):
        counts = Counter(i["fingerprint"] for i in right)
        result = []
        for item in left:
            key = item["fingerprint"]
            if counts[key]:
                counts[key] -= 1
            else:
                result.append({k: v for k, v in item.items() if k != "fingerprint"})
        return result
    left = {r["name"]: r["bytes"] for r in previous.get("top_fields", [])}
    right = {r["name"]: r["bytes"] for r in current.get("top_fields", [])}
    old_upload = previous.get("outbound_wire_bytes")
    new_upload = current.get("outbound_wire_bytes")
    old_compressed = compressed_packet_size(previous)
    new_compressed = compressed_packet_size(current)
    return {"upload_delta": new_upload - old_upload if old_upload is not None and new_upload is not None else None,
            "compressed_delta": new_compressed - old_compressed if old_compressed is not None and new_compressed is not None else None,
            "wire_delta": current.get("wire_bytes", 0) - previous.get("wire_bytes", 0),
            "decoded_delta": current.get("decoded_bytes", 0) - previous.get("decoded_bytes", 0),
            "added": unmatched(new, old), "removed": unmatched(old, new),
            "fields": [{"name": k, "before": left.get(k, 0), "after": right.get(k, 0)}
                       for k in sorted(left.keys() | right.keys()) if left.get(k) != right.get(k)]}
