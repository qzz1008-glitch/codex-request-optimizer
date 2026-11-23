"""Use the HTML header as the title bar while retaining native resize borders."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os

from .window_subclass import WM_NCDESTROY, window_subclass_api


WS_CAPTION = 0x00C00000
GWL_STYLE = -16
SWP_NOSIZE = 0x01
SWP_NOMOVE = 0x02
SWP_FRAMECHANGED = 0x20
WM_WINDOWPOSCHANGING = 0x46
MONITOR_DEFAULTTONEAREST = 2


class WindowPos(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", wintypes.HWND),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class WindowInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcWindow", wintypes.RECT), ("rcClient", wintypes.RECT),
                ("dwStyle", wintypes.DWORD), ("dwExStyle", wintypes.DWORD), ("dwWindowStatus", wintypes.DWORD),
                ("cxWindowBorders", wintypes.UINT), ("cyWindowBorders", wintypes.UINT),
                ("atomWindowType", wintypes.WORD), ("wCreatorVersion", wintypes.WORD)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _fit_maximized_window(user, hwnd, position):
    geometry_flags = SWP_NOMOVE | SWP_NOSIZE
    if position.flags & geometry_flags == geometry_flags:
        return
    info = WindowInfo(ctypes.sizeof(WindowInfo))
    if not user.GetWindowInfo(hwnd, ctypes.byref(info)) or info.dwStyle & WS_CAPTION:
        return

    frame = info.rcWindow
    if position.flags & SWP_NOMOVE:
        x, y = frame.left, frame.top
    else:
        x, y = position.x, position.y
    if position.flags & SWP_NOSIZE:
        width, height = frame.right - frame.left, frame.bottom - frame.top
    else:
        width, height = position.cx, position.cy
    proposed = wintypes.RECT(x, y, x + width, y + height)
    monitor = user.MonitorFromRect(ctypes.byref(proposed), MONITOR_DEFAULTTONEAREST)
    monitor_info = MonitorInfo(ctypes.sizeof(MonitorInfo))
    if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
        return

    # Fit the visible client area to the work area and retain the invisible resize border.
    client, work = info.rcClient, monitor_info.rcWork
    left = work.left - (client.left - frame.left)
    top = work.top - (client.top - frame.top)
    right = work.right + (frame.right - client.right)
    bottom = work.bottom + (frame.bottom - client.bottom)
    position.x, position.y = left, top
    position.cx, position.cy = right - left, bottom - top
    position.flags &= ~geometry_flags


class WindowChrome:
    def __init__(self):
        self._window = None
        self._enabled = False
        self._subclass_hwnd = None

    def _bind(self, window):
        self._window = window
        window.events.before_show += self._before_show
        window.events.shown += self._install
        window.events.maximized += self._notify
        window.events.restored += self._notify

    def _before_show(self):
        self._install()
        if not self._enabled or self._subclass_hwnd:
            return

        # Install on the UI thread before WinForms processes its first maximize.
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.IsZoomed.argtypes = user.IsIconic.argtypes = [wintypes.HWND]
        user.IsZoomed.restype = user.IsIconic.restype = wintypes.BOOL
        user.GetWindowInfo.argtypes = [wintypes.HWND, ctypes.POINTER(WindowInfo)]
        user.GetWindowInfo.restype = wintypes.BOOL
        user.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        user.MonitorFromRect.restype = wintypes.HMONITOR
        user.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
        user.GetMonitorInfoW.restype = wintypes.BOOL
        common, callback_type = window_subclass_api()

        @callback_type
        def window_proc(hwnd, message, wparam, lparam, subclass_id, _data):
            if message == WM_NCDESTROY:
                common.RemoveWindowSubclass(hwnd, self._position_callback, subclass_id)
                self._subclass_hwnd = None
            result = common.DefSubclassProc(hwnd, message, wparam, lparam)
            if message == WM_WINDOWPOSCHANGING and lparam and user.IsZoomed(hwnd) and not user.IsIconic(hwnd):
                # WinForms validates WINDOWPOS first. Apply the work area to the final proposal.
                _fit_maximized_window(user, hwnd, WindowPos.from_address(lparam))
            return result

        hwnd = int(self._window.native.Handle.ToInt64())
        self._position_callback = window_proc
        if not common.SetWindowSubclass(hwnd, window_proc, 1, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self._subclass_hwnd = hwnd

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
        flags = SWP_FRAMECHANGED | SWP_NOSIZE | SWP_NOMOVE | 0x04 | 0x10
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
