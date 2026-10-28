"""Synchronize HTTP fixtures with the relay's post-response bookkeeping."""
import time


HTTP_TIMEOUT = 15


def wait_for_idle(*servers, timeout=HTTP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while any(server.active_requests for server in servers):
        if time.monotonic() >= deadline:
            raise TimeoutError("Relay fixture still has active requests during cleanup")
        time.sleep(.01)
