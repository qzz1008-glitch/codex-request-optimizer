"""Read-only process identity for failure diagnostics; never reads credentials."""
from functools import lru_cache
import os
from pathlib import Path
import sys


def process_identity():
    return dict(_identity(os.getpid(), os.getppid(), sys.executable))


@lru_cache(maxsize=4)
def _identity(pid, parent_pid, executable):
    result = {"pid": pid, "parent_pid": parent_pid, "executable": Path(executable).name}
    if os.name != "nt":
        return result
    import ctypes
    from ctypes import wintypes as w
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        kernel.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        security.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        security.GetTokenInformation.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD))
        security.GetUserNameW.argtypes = (w.LPWSTR, ctypes.POINTER(w.DWORD))
        kernel.ProcessIdToSessionId.argtypes = (w.DWORD, ctypes.POINTER(w.DWORD))
        name, length = ctypes.create_unicode_buffer(257), w.DWORD(257)
        if security.GetUserNameW(name, ctypes.byref(length)):
            result["username"] = name.value
        session = w.DWORD()
        if kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
            result["windows_session_id"] = session.value
        token = w.HANDLE()
        if security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            try:
                for kind, field in ((10, "logon_id"), (20, "elevated"), (29, "app_container")):
                    data, size = ctypes.create_string_buffer(256), w.DWORD()
                    if security.GetTokenInformation(token, kind, data, len(data), ctypes.byref(size)):
                        result[field] = ("0x" + format(int.from_bytes(data.raw[8:16], "little"), "x")
                                         if kind == 10 else bool(int.from_bytes(data.raw[:4], "little")))
            finally:
                kernel.CloseHandle(token)
        parent = kernel.OpenProcess(0x1000, False, parent_pid)
        if parent:
            try:
                name, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(parent, 0, name, ctypes.byref(length)):
                    result["parent_executable"] = Path(name.value).name
            finally:
                kernel.CloseHandle(parent)
    except (OSError, AttributeError) as exc:
        result["identity_error"] = type(exc).__name__
    return result
