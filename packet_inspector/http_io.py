"""Bounded cleanup of rejected local control requests."""
from __future__ import annotations


def discard_small_body(handler):
    """Avoid a TCP reset hiding an error response when a small body is unread."""
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(lengths) != 1:
        return
    try:
        length = int(lengths[0])
    except ValueError:
        return
    if not 0 < length <= 8192:
        return
    timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(.25)
        handler.rfile.read(length)
    except OSError:
        pass
    finally:
        handler.connection.settimeout(timeout)
