"""Display-only port of Codex's model-visible history token estimator.

Reference: codex-rs/core/src/context_manager/history.rs
(estimate_item_token_count / estimate_token_count_with_base_instructions),
codex-rs/protocol/src/protocol.rs (estimate_compaction_content_length), and
codex-rs/utils/string/src/truncate.rs. This is a coarse estimate, never usage.
"""
import json
import hashlib

METHOD = "codex_model_visible_v1"
SOURCE = "codex_estimate"
IMAGE_BYTES = 7373


def overflow_applied(packet):
    calls = packet.get("overflow_compaction_calls")
    return type(calls) is int and calls > 0 or packet.get("overflow_cache_hit") is True


def estimate_usage(packet):
    """Only use an explicitly labelled estimate for an actually replaced input."""
    value = packet.get("request_token_estimate")
    if (overflow_applied(packet) and isinstance(value, dict) and value.get("state") == "available"
            and value.get("source") == SOURCE and value.get("estimated") is True
            and type(value.get("input_tokens")) is int and value["input_tokens"] >= 0):
        return dict(value)
    return None


def unavailable(reason):
    return {"state": "unavailable", "source": SOURCE, "estimated": True, "method": METHOD, "reason": reason}


def text_bytes(value):
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def part_bytes(part):
    if not isinstance(part, dict):
        raise ValueError("unsupported_content")
    kind = part.get("type")
    if kind in {"input_text", "output_text", "text"}:
        return text_bytes(part.get("text"))
    if kind == "encrypted_content":
        return (text_bytes(part.get("encrypted_content")) * 9 + 15) // 16
    if kind == "input_image":
        if part.get("detail") == "original":
            if part.get("file_id"):
                return 40_000  # Native maximum: 10,000 original-detail patches.
            # Matching the native inline-original estimate requires decoding the
            # image. Keep this observer free of media decoding and file reads.
            raise ValueError("original_image_dimensions_unavailable")
        return IMAGE_BYTES
    if kind == "input_audio":
        raise ValueError("audio_duration_unavailable")
    raise ValueError("unsupported_content")


def content_bytes(content):
    if isinstance(content, str):
        return text_bytes(content)
    if isinstance(content, list):
        return sum(part_bytes(part) for part in content)
    raise ValueError("unsupported_content")


def item_bytes(item):
    if not isinstance(item, dict):
        raise ValueError("unsupported_input")
    kind = item.get("type", "message" if "role" in item else None)
    if kind == "message":
        return content_bytes(item.get("content", []))
    if kind == "agent_message":
        return (text_bytes(item.get("author")) + text_bytes(item.get("recipient"))
                + content_bytes(item.get("content", [])))
    if kind == "reasoning":
        return max(0, text_bytes(item.get("encrypted_content")) * 3 // 4 - 650)
    if kind in {"compaction", "context_compaction"}:
        return (text_bytes(item.get("encrypted_content")) + 2) // 3
    if kind in {"function_call", "custom_tool_call"}:
        return (text_bytes(item.get("name")) + text_bytes(item.get("namespace") or "functions")
                + text_bytes(item.get("arguments" if kind == "function_call" else "input")))
    if kind in {"function_call_output", "custom_tool_call_output"}:
        return (content_bytes(item.get("output", "")) + text_bytes(item.get("call_id"))
                + text_bytes(item.get("name"))
                + (text_bytes(item.get("namespace")) if kind == "function_call_output" else 0))
    if kind in {"additional_tools", "tool_search_output"}:
        return json_bytes(item.get("tools", []))
    if kind == "tool_search_call":
        return json_bytes(item.get("arguments"))
    if kind in {"local_shell_call", "web_search_call"}:
        return json_bytes(item["action"]) if item.get("action") is not None else 0
    if kind == "image_generation_call":
        return text_bytes(item.get("revised_prompt")) + (IMAGE_BYTES if item.get("result") else 0)
    if kind in {"configuration_update", "compaction_trigger"}:
        return 0
    raise ValueError("unsupported_input_type")


def recorded_instructions(item):
    if not isinstance(item, dict):
        return False
    if item.get("type") == "additional_tools":
        return True
    metadata = item.get("internal_chat_message_metadata_passthrough")
    content = item.get("content")
    return (item.get("role") == "developer" and isinstance(metadata, dict)
            and metadata.get("content_item_kinds") == ["model.base_instructions"]
            and isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "input_text")


def estimate_request_tokens(body, *, retained_copy=False):
    """Count content before summary replacement, excluding transport envelopes.

    Like Codex's local estimator, root request settings and outer tool schemas
    are not history items. Tool declarations carried in history are counted.
    """
    try:
        if not isinstance(body, dict) or body.get("previous_response_id") or body.get("conversation"):
            return unavailable("incomplete_input")
        items = body.get("input")
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if not isinstance(items, list):
            return unavailable("unsupported_input")
        instructions = 0 if any(recorded_instructions(item) for item in items) else (text_bytes(body.get("instructions")) + 3) // 4
        count = instructions + sum((item_bytes(item) + 3) // 4 for item in items)
        result = {"state": "available", "source": SOURCE, "estimated": True, "method": METHOD,
                  "input_tokens": count}
        if retained_copy:
            result["retained_copy"] = True
        return result
    except (TypeError, ValueError, UnicodeError) as exc:
        return unavailable(str(exc) if isinstance(exc, ValueError) else "invalid_input")


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def request_basis(body):
    """A small durable basis; no text, tool payloads, or per-item hashes."""
    result = estimate_request_tokens(body, retained_copy=True)
    if result["state"] != "available":
        return result
    items = body.get("input")
    if not isinstance(items, list):
        return result
    # Exclude transport/session labels, keeping semantic request settings.
    scope = {k: v for k, v in body.items() if k not in {
        "input", "metadata", "client_metadata", "prompt_cache_key", "stream", "store"}}
    result.update(coarse_input_tokens=result["input_tokens"], history_items=len(items),
                  history_sha256=fingerprint(items), scope_sha256=fingerprint(scope))
    return result


def calibrate(basis, body, previous, actual_tokens, packet_id):
    """Anchor at authentic unchanged input, estimate only the appended content.

    This follows Codex's observed-usage-plus-local-estimate approach without
    trusting the synthetic usage used to request native compaction. A context
    edit, model/configuration change or compaction invalidates the prefix.
    """
    if not (basis.get("state") == previous.get("state") == "available"
            and basis.get("method") == previous.get("method") == METHOD
            and type(actual_tokens) is int and actual_tokens >= 0
            and type(previous.get("history_items")) is int
            and previous.get("scope_sha256") == basis.get("scope_sha256")
            and type(basis.get("coarse_input_tokens")) is int
            and type(previous.get("coarse_input_tokens")) is int):
        return basis
    count = previous["history_items"]
    items = body.get("input")
    delta = basis["coarse_input_tokens"] - previous["coarse_input_tokens"]
    if (not isinstance(items, list) or not 0 <= count <= len(items) or delta < 0
            or fingerprint(items[:count]) != previous.get("history_sha256")):
        return basis
    return {**basis, "input_tokens": actual_tokens + delta, "basis_packet_id": packet_id,
            "basis_input_tokens": actual_tokens, "appended_estimated_tokens": delta}
