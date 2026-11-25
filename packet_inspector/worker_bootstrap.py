"""Release a Windows startup worker only after its router owns the entry port."""
from contextlib import ExitStack, contextmanager
import re
import secrets


PREPARED_WORKER_ENV = "CODEX_INSPECTOR_PREPARED_WORKER"
WORKER_GATE_ENV = "CODEX_INSPECTOR_WORKER_GATE"


def _events(ticket):
    if not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise ValueError("invalid worker startup ticket")
    prefix = "Local\\CodexRequestOptimizer-WorkerStart-" + ticket
    return prefix + "-cancel", prefix + "-start"


@contextmanager
def hold_worker_start():
    import win32event

    ticket = secrets.token_hex(16)
    with ExitStack() as stack:
        handles = []
        for name in _events(ticket):
            event = win32event.CreateEvent(None, True, False, name)
            stack.callback(event.Close)
            handles.append(event)
        try:
            yield ticket
        finally:
            win32event.SetEvent(handles[0])


def permit_worker_start(ticket):
    import pywintypes
    import win32event

    try:
        event = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, _events(ticket)[1])
    except pywintypes.error as exc:
        if exc.winerror == 2:  # The launcher already released the startup events.
            return False
        raise
    try:
        win32event.SetEvent(event)
        return True
    finally:
        event.Close()


def wait_for_worker_permission(ticket, seconds):
    import pywintypes
    import win32con
    import win32event

    try:
        with ExitStack() as stack:
            handles = []
            for name in _events(ticket):
                event = win32event.OpenEvent(win32con.SYNCHRONIZE, False, name)
                stack.callback(event.Close)
                handles.append(event)
            # Cancellation comes first if both events were signaled.
            signaled = win32event.WaitForMultipleObjects(handles, False, int(seconds * 1000))
            return signaled == win32event.WAIT_OBJECT_0 + 1
    except pywintypes.error as exc:
        if exc.winerror == 2:
            return False
        raise


@contextmanager
def watch_worker_process(pid):
    import win32api
    import win32con
    import win32event
    import win32process

    handle = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        def poll():
            if win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0:
                return win32process.GetExitCodeProcess(handle)
            return None

        yield poll
    finally:
        handle.Close()
