"""One sharing role per machine, regardless of port, profile or data directory."""
import os
from pathlib import Path
import tempfile
import threading

from .locking import file_lock
from .sharing_auth import SharingError

BUSY = "本机已有实例正在借出或借入连接，请先停止共享或断开借入。"


class MachineSharingRole:
    def __init__(self, name="CodexRequestOptimizer-SharingRole-v2"):
        self.name = name
        self.guard = threading.Lock()
        self.resource = None

    def acquire(self):
        with self.guard:
            if self.resource is not None:
                raise SharingError(BUSY)
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                kernel.CreateMutexW.restype = wintypes.HANDLE
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle.restype = wintypes.BOOL
                ctypes.set_last_error(0)
                handle = kernel.CreateMutexW(None, False, "Global\\" + self.name)
                if not handle:
                    raise SharingError(BUSY)
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    kernel.CloseHandle(handle)
                    raise SharingError(BUSY)
                # Hold the named object's lifetime, not thread-affine mutex
                # ownership. A later HTTP thread can release it safely.
                self.resource = (kernel, handle)
            else:
                lock = file_lock(Path(tempfile.gettempdir()) / (self.name + ".lock"), blocking=False)
                try:
                    lock.__enter__()
                except OSError:
                    raise SharingError(BUSY) from None
                self.resource = (None, lock)

    def close(self):
        with self.guard:
            resource, self.resource = self.resource, None
            if resource is not None:
                kernel, handle = resource
                if kernel is not None:
                    kernel.CloseHandle(handle)
                else:
                    handle.__exit__(None, None, None)
