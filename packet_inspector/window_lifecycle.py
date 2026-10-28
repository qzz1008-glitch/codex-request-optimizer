"""Keep the desktop window in the tray until the service exits or upgrades it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import threading
import time

from .taskbar import TaskbarAttention


EXIT_PROPERTY = "CodexRequestOptimizer.ExitWindow"


def window_control_prefix(data_dir):
    identity = hashlib.sha256(str(Path(data_dir).resolve()).casefold().encode()).hexdigest()[:20]
    return "Local\\CodexPacketInspector-" + identity


def request_window_exit(data_dir):
    """Signal only this optimizer frontend; the forwarding process stays alive."""
    return _signal_window(data_dir, "-exit")


def request_tray_panel(data_dir):
    """Raise an existing panel without launching or extracting another EXE."""
    return _signal_window(data_dir, "-tray-panel")


def tray_panel_host_running(data_dir):
    """Check for the desktop host without showing either of its windows."""
    return _signal_window(data_dir, "-tray-panel", notify=False)


def tray_panel_ready(data_dir):
    """A running host may still be extracting/loading its first WebView page."""
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenEventW(0x00100000, False, window_control_prefix(data_dir) + "-tray-ready")  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


def _signal_window(data_dir, suffix, *, notify=True):
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenEventW(0x0002, False, window_control_prefix(data_dir) + suffix)
    if not handle:
        return False
    try:
        return bool(kernel.SetEvent(handle)) if notify else True
    finally:
        kernel.CloseHandle(handle)


def window_exit_requested(window):
    if os.name != "nt":
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user.GetPropW.restype = wintypes.HANDLE
    return bool(user.GetPropW(int(window.native.Handle.ToInt64()), EXIT_PROPERTY))


class WindowLifecycle:
    def __init__(self, window, can_hide):
        self.window, self.can_hide = window, can_hide
        self.exiting = threading.Event()
        self.closed = threading.Event()
        self.hidden = threading.Event()
        self.attention = TaskbarAttention(window)
        window.events.closing += self.on_closing
        window.events.closed += self.closed.set
        window._optimizer_exit = self.exit
        window._optimizer_lifecycle = self

    def on_closing(self):
        if self.exiting.is_set() or window_exit_requested(self.window):
            return True
        if self.can_hide():
            self.window.hide()
            self.hidden.set()
        else:
            # A service started without a tray must never leave an unreachable UI.
            self.window.minimize()
        return False

    def show(self):
        if self.closed.is_set():
            return
        if str(self.window.native.WindowState) == "Minimized":
            self.window.restore()
        self.window.show()
        self.attention.clear()
        self.hidden.clear()

    def wait_loaded(self, loaded, timeout=25):
        deadline = time.monotonic() + timeout
        while not self.exiting.is_set() and not self.closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if loaded.wait(min(.1, remaining)):
                return not self.exiting.is_set() and not self.closed.is_set()
        return False

    def exit(self):
        self.exiting.set()
        if not self.closed.is_set():
            self.window.destroy()
