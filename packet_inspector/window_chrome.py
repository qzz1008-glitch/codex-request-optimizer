"""Use the HTML header as the title bar while retaining native resize borders."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os


WS_CAPTION = 0x00C00000
GWL_STYLE = -16
SWP_FRAMECHANGED = 0x20


class WindowChrome:
    def __init__(self):
        self._window = None
        self._enabled = False

    def _bind(self, window):
        self._window = window
        window.events.before_show += self._install
        window.events.shown += self._install
        window.events.maximized += self._notify
        window.events.restored += self._notify

    def _install(self):
        if os.name != "nt":
            return
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongW.restype = ctypes.c_long
        user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user.SetWindowLongW.restype = ctypes.c_long
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos.restype = wintypes.BOOL
        hwnd = int(self._window.native.Handle.ToInt64())
        style = user.GetWindowLongW(hwnd, GWL_STYLE)
        if not style & WS_CAPTION:
            self._enabled = True
            return
        # Keep the system resize border and window commands; remove only the
        # duplicate caption. WinForms can reapply it when showing/restoring.
        ctypes.set_last_error(0)
        previous = user.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_CAPTION)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        flags = SWP_FRAMECHANGED | 0x01 | 0x02 | 0x04 | 0x10
        if not user.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags):
            user.SetWindowLongW(hwnd, GWL_STYLE, style)
            raise ctypes.WinError(ctypes.get_last_error())
        self._enabled = True

    def window_state(self):
        return {"enabled": self._enabled,
                "maximized": bool(self._window and str(self._window.native.WindowState) == "Maximized")}

    def minimize_window(self):
        if self._enabled:
            self._window.minimize()

    def toggle_maximize(self):
        if self._enabled:
            if self.window_state()["maximized"]:
                self._window.restore()
            else:
                self._window.maximize()
        return self.window_state()

    def close_window(self):
        if self._enabled:
            # WindowLifecycle owns closing, including the existing tray behavior.
            self._window.destroy()

    def _notify(self):
        self._install()
        if self._enabled:
            try:
                self._window.evaluate_js(
                    "window.dispatchEvent(new CustomEvent('desktop-window-state', {detail:"
                    + json.dumps(self.window_state()) + "})); true")
            except Exception:
                pass  # A resize can happen while the document is reloading.
