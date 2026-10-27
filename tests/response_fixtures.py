"""Synthetic Responses fixtures; no real model or credentials."""

import io

import json

from email.message import Message

def frame(kind, **data):
    return b"data: " + json.dumps({"type": kind, **data}).encode() + b"\n\n"

class Response:
    def __init__(self, data, status=200, content_type="text/event-stream", extra=None, chunk=65536):
        self.data = io.BytesIO(data)
        self.status, self.reason = status, "Test response"
        self.headers = Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        for key, value in (extra or {}).items():
            self.headers[key] = value
        self.chunk = chunk
        self.closed = False

    def read1(self, size=65536):
        return self.data.read(min(size, self.chunk))

    def read(self, size=-1):
        return self.data.read(size)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

CREATED = frame("response.created", response={"id":"test","status":"in_progress"})

FAILED = frame("response.failed", response={"error":{"code":"server_is_overloaded"}})

SUCCESS = frame("response.completed", response={"status":"completed","output":[]})
