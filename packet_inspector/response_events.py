"""Read-only Responses event classification shared by rewriting and diagnostics."""

import json
import re

MAX_OBSERVATION_BYTES = 1024 * 1024

CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."

CAPACITY_CODES = {"server_is_overloaded", "server_overloaded", "model_overloaded", "model_at_capacity"}

HANDSHAKE_EVENTS = {"response.created", "response.in_progress", "response.queued", "ping", "keepalive"}

TERMINAL_STATUSES = {"response.completed": "completed", "response.failed": "failed",
                     "response.incomplete": "incomplete", "response.cancelled": "cancelled"}
TERMINAL_EVENTS = {*TERMINAL_STATUSES, "error"}

EVENT_BOUNDARY = re.compile(br"\r?\n\r?\n")


def response_media_type(headers):
    kind = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return "application/json" if kind.startswith("application/") and kind.endswith("+json") else kind


def sse_fields(frame):
    """Read standard SSE fields, including multiline data and the last event name.

    Callers bound the frame before parsing. Unknown fields remain untouched in
    the original stream; this function only supplies an inspection copy.
    """
    data, name = [], ""
    for line in frame.removeprefix(b"\xef\xbb\xbf").splitlines():
        field, _, value = line.partition(b":")
        value = value.removeprefix(b" ")
        if field == b"data":
            data.append(value)
        elif field == b"event":
            name = value.decode("utf8")
    return b"\n".join(data), name


def sse_payload(data, name=""):
    if not data or data.strip() == b"[DONE]":
        return None
    value = json.loads(data)
    if isinstance(value, dict) and "type" not in value and name:
        value["type"] = name
    return value


def decode_sse(frame):
    return sse_payload(*sse_fields(frame))


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
    as unknown for diagnostics. Only capacity_error identifies the explicit
    capacity errors eligible for native retry advice.
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
    if not kind and isinstance(status, str) and status in TERMINAL_STATUSES.values():
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
