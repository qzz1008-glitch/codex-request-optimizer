"""Read-only Windows desktop lifetime detection, without launching a shell."""
import os
from pathlib import Path


def desktop_started_at_ns():
    """Return the oldest running Codex desktop process's Unix creation time.

    An absent desktop or incomplete process inspection is inconclusive. PID
    changes, renderer reloads and unrelated command-line Codex instances do
    not prove that the desktop has restarted.
    """
    if os.name != "nt":
        return None
    try:
        return _windows_started_at_ns()
    except (OSError, AttributeError):
        return None


def _windows_started_at_ns():
    import ctypes
    from ctypes import wintypes as w

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("name", w.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = (w.HANDLE, ctypes.POINTER(ProcessEntry))
        function.restype = w.BOOL
    kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    kernel.ProcessIdToSessionId.argtypes = (w.DWORD, ctypes.POINTER(w.DWORD))
    kernel.ProcessIdToSessionId.restype = w.BOOL
    kernel.GetPackageFamilyName.argtypes = (w.HANDLE, ctypes.POINTER(w.UINT), w.LPWSTR)
    kernel.GetPackageFamilyName.restype = w.LONG
    kernel.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.GetProcessTimes.argtypes = (w.HANDLE, *(ctypes.POINTER(w.FILETIME),) * 4)
    kernel.GetProcessTimes.restype = w.BOOL

    session = w.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    starts, desktop = [], False
    try:
        row = ProcessEntry()
        row.size = ctypes.sizeof(row)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(row))
        while found:
            if row.name.casefold() in {"chatgpt.exe", "codex.exe"}:
                current = w.DWORD()
                if not kernel.ProcessIdToSessionId(row.pid, ctypes.byref(current)):
                    if ctypes.get_last_error() != 87:  # A process exited during enumeration.
                        raise ctypes.WinError(ctypes.get_last_error())
                elif current.value == session.value:
                    process = kernel.OpenProcess(0x1000, False, row.pid)  # QUERY_LIMITED_INFORMATION
                    if not process:
                        if ctypes.get_last_error() != 87:
                            raise ctypes.WinError(ctypes.get_last_error())
                    else:
                        try:
                            family, length = ctypes.create_unicode_buffer(256), w.UINT(256)
                            result = kernel.GetPackageFamilyName(process, ctypes.byref(length), family)
                            if result not in {0, 15700}:  # APPMODEL_ERROR_NO_PACKAGE
                                raise ctypes.WinError(result)
                            image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
                            if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                                raise ctypes.WinError(ctypes.get_last_error())
                            path = Path(image.value)
                            embedded_cli = (path.parent / "resources" / "codex.exe").is_file()
                            packaged = family.value.casefold().startswith("openai.codex_")
                            if packaged or embedded_cli:
                                created, exited, system, user = (w.FILETIME() for _ in range(4))
                                if not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                                              ctypes.byref(system), ctypes.byref(user)):
                                    raise ctypes.WinError(ctypes.get_last_error())
                                started = ((created.dwHighDateTime << 32) | created.dwLowDateTime)
                                starts.append((started - 116444736000000000) * 100)
                                desktop |= row.name.casefold() == "chatgpt.exe" or embedded_cli
                        finally:
                            kernel.CloseHandle(process)
            found = kernel.Process32NextW(snapshot, ctypes.byref(row))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    return min(starts) if desktop and starts else None
