"""Keep the desktop window in the tray until the service exits or upgrades it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


EXIT_PROPERTY = "CodexRequestOptimizer.ExitWindow"


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
        self.hidden.clear()

    def exit(self):
        self.exiting.set()
        if not self.closed.is_set():
            self.window.destroy()
