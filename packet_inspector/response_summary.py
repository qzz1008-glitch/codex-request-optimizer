"""Bounded, passive excerpts of visible Responses output, kept with each packet."""
from __future__ import annotations

import json
import re

from .analyzer import KEY_PATTERN, clean_text, encode

MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_TEXT = 500
MAX_TOOLS = 8
MAX_SUMMARY_BYTES = 4096
BOUNDARY = re.compile(br"\r?\n\r?\n")
CREDENTIALS = re.compile(KEY_PATTERN.pattern, re.I | re.ASCII)
TERMINALS = {"response.completed": "completed", "response.failed": "failed",
             "response.incomplete": "incomplete", "response.cancelled": "cancelled"}


def safe_text(value):
    # ASCII token boundaries also catch a key adjacent to Chinese text.
    return clean_text(CREDENTIALS.sub("[credential redacted]", value))


class ResponseSummary:
    def __init__(self, response):
        self.mode = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if self.mode.startswith("application/") and self.mode.endswith("+json"):
            self.mode = "application/json"
        self.enabled = response.headers.get("Content-Encoding", "identity").lower() in {"", "identity"} and self.mode in {
            "", "text/event-stream", "application/json"}
        self.pending = bytearray()
        self.skipping = False
        self.text = ""
        self.text_truncated = False
        self.tools = []
        self.tools_truncated = False
        self.response_id = ""
        self.status = ""
        self.have_delta = False
        self.hidden_output = set()
        self.suppress_deltas = False
        self.observation_limited = not self.enabled

    def _text(self, text):
        if not isinstance(text, str) or not text:
            return
        # Redact before persisting; media, tool arguments and reasoning are never extracted.
        safe = safe_text(text)
        remaining = max(0, MAX_TEXT - len(self.text))
        self.text += safe[:remaining]
        self.text_truncated |= len(safe) > remaining

    def _item(self, item, *, text=True):
        if not isinstance(item, dict):
            return
        kind = item.get("type", "")
        if isinstance(kind, str) and kind.endswith("_call"):
            name = item.get("name") or kind
            if isinstance(name, str):
                name = safe_text(name)[:64]
                if name and name not in self.tools:
                    if len(self.tools) < MAX_TOOLS:
                        self.tools.append(name)
                    else:
                        self.tools_truncated = True
        if text and kind == "message" and item.get("role", "assistant") == "assistant" and item.get("channel") != "analysis":
            content = item.get("content")
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}:
                    value = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
                    if isinstance(value, str) and value:
                        if self.text and not self.text.endswith("\n"):
                            self._text("\n")
                        self._text(value)

    def _consume(self, value, *, event=True):
        if not isinstance(value, dict):
            return
        kind = value.get("type", "") if event else ""
        response = value.get("response") if event else value
        if isinstance(response, dict):
            identity = response.get("id")
            if isinstance(identity, str):
                self.response_id = safe_text(identity)[:128]
            status = response.get("status")
            if status in {"completed", "failed", "incomplete", "cancelled", "in_progress", "queued"}:
                self.status = status
            output = response.get("output")
            if isinstance(output, list) and (not event or kind in TERMINALS):
                # The final output is authoritative and must not duplicate streamed deltas.
                previous = self.text, self.text_truncated
                self.text = ""; self.text_truncated = False
                for item in output:
                    self._item(item)
                if not self.text and not output:
                    self.text, self.text_truncated = previous
            if not event and not self.status:
                if isinstance(response.get("error"), dict):
                    self.status = "failed"
                elif isinstance(response.get("usage"), dict) or isinstance(output, list):
                    self.status = "completed"
        if kind in TERMINALS:
            self.status = TERMINALS[kind]
        elif kind == "error":
            self.status = "failed"
        elif kind in {"response.output_text.delta", "response.refusal.delta"} and not self.suppress_deltas and value.get("channel") != "analysis" and value.get("output_index") not in self.hidden_output:
            self.have_delta = True
            self._text(value.get("delta"))
        elif kind in {"response.output_text.done", "response.refusal.done"} and not self.have_delta and not self.suppress_deltas and value.get("channel") != "analysis" and value.get("output_index") not in self.hidden_output:
            self._text(value.get("text") or value.get("refusal"))
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            item = value.get("item")
            index = value.get("output_index")
            if isinstance(item, dict) and (item.get("type") == "reasoning" or item.get("channel") == "analysis"):
                if type(index) is int and len(self.hidden_output) < 128:
                    self.hidden_output.add(index)
                elif index not in self.hidden_output:
                    self.suppress_deltas = True; self.observation_limited = True
            self._item(item, text=not self.have_delta and kind.endswith(".done"))

    def _frame(self, data):
        lines = data.removeprefix(b"\xef\xbb\xbf").splitlines()
        body = b"\n".join(line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:"))
        if body and body != b"[DONE]":
            try:
                value = json.loads(body)
                if isinstance(value, dict) and not value.get("type"):
                    event = next((line[6:].strip() for line in lines if line.startswith(b"event:")), b"")
                    if event:
                        value = {**value, "type": event.decode("utf8")}
                self._consume(value)
            except (ValueError, UnicodeError):
                self.observation_limited = True

    def feed(self, chunk):
        if not self.enabled:
            return
        try:
            self.pending.extend(chunk)
            if not self.mode:
                if len(self.pending) > MAX_EVENT_BYTES:
                    self.pending.clear(); self.enabled = False; self.observation_limited = True
                    return
                prefix = bytes(self.pending).removeprefix(b"\xef\xbb\xbf").lstrip()
                if len(self.pending) < 3 or not prefix:
                    return
                self.mode = "application/json" if prefix.startswith(b"{") else "text/event-stream"
            if self.mode == "application/json":
                if len(self.pending) > MAX_EVENT_BYTES:
                    self.pending.clear(); self.enabled = False; self.observation_limited = True
                return
            while match := BOUNDARY.search(self.pending):
                frame = bytes(self.pending[:match.start()])
                del self.pending[:match.end()]
                if not self.skipping and len(frame) <= MAX_EVENT_BYTES:
                    self._frame(frame)
                else:
                    self.observation_limited = True
                self.skipping = False
            if len(self.pending) > MAX_EVENT_BYTES:
                del self.pending[:-3]; self.skipping = True; self.observation_limited = True
        except (ValueError, TypeError, UnicodeError):
            self.pending.clear(); self.enabled = False; self.observation_limited = True

    def finish(self, relay_state):
        if self.enabled and self.pending and not self.skipping:
            try:
                if self.mode == "application/json":
                    self._consume(json.loads(self.pending), event=False)
                else:
                    self._frame(bytes(self.pending))
            except (ValueError, TypeError, UnicodeError):
                self.observation_limited = True
        self.pending.clear()
        if self.status not in {"completed", "failed", "incomplete", "cancelled"}:
            self.status = "interrupted" if relay_state != "complete" else "unknown"
        text = safe_text(self.text)
        result = {"version": 1, "status": self.status, "text": text[:MAX_TEXT],
                  "text_truncated": self.text_truncated or len(text) > MAX_TEXT, "tools": list(self.tools),
                  "tools_truncated": self.tools_truncated}
        if self.response_id:
            result["response_id"] = self.response_id
        if self.observation_limited:
            result["observation_limited"] = True
        while len(encode(result)) > MAX_SUMMARY_BYTES:
            if result["tools"]:
                result["tools"].pop(); result["tools_truncated"] = True
            else:
                result["text"] = result["text"][:-100]; result["text_truncated"] = True
        return result
