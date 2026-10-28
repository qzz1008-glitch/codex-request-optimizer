"""A reusable WebView tray panel owned by the existing desktop process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import threading


def popup_position(anchor, work_area, size):
    left, top, right, bottom = work_area
    width, height = size
    return (max(left + 6, min(anchor[0] - width, right - width - 6)),
            max(top + 6, min(anchor[1] - height - 8, bottom - height - 6)))


def popup_anchor():
    if os.name != "nt":
        return (1920, 1080), (0, 0, 1920, 1080)
    user = ctypes.WinDLL("user32", use_last_error=True)
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user.MonitorFromPoint.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    point, monitor = wintypes.POINT(), MonitorInfo()
    monitor.cbSize = ctypes.sizeof(monitor)
    if not user.GetCursorPos(ctypes.byref(point)) or not user.GetMonitorInfoW(
            user.MonitorFromPoint(point, 2), ctypes.byref(monitor)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (point.x, point.y), (monitor.work.left, monitor.work.top, monitor.work.right, monitor.work.bottom)


def place_panel(window, anchor):
    if os.name != "nt":
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    handle, rect = int(window.native.Handle.ToInt64()), wintypes.RECT()
    if user.GetWindowRect(handle, ctypes.byref(rect)):
        x, y = popup_position(*anchor, (rect.right - rect.left, rect.bottom - rect.top))
        user.SetWindowPos(handle, None, x, y, 0, 0, 0x01 | 0x04 | 0x10)


def resize_panel_window(window, width, height):
    if os.name != "nt":
        window.resize(width, height)
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetDpiForWindow.argtypes = [wintypes.HWND]
    user.GetDpiForWindow.restype = wintypes.UINT
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    from System import Action
    form = window.native
    def resize():
        handle = int(form.Handle.ToInt64())
        scale = (user.GetDpiForWindow(handle) or 96) / 96
        # pywebview's resize uses SWP_SHOWWINDOW, which would expose a hidden panel.
        if not user.SetWindowPos(handle, None, 0, 0, int(width * scale), int(height * scale), 0x02 | 0x04 | 0x10):
            raise ctypes.WinError(ctypes.get_last_error())
        form.PerformLayout()
    if form.InvokeRequired:
        form.Invoke(Action(resize))
    else:
        resize()


def configure_panel(window, deactivate):
    if os.name != "nt":
        return
    from System import Action
    form = window.native
    def configure():
        form.ShowInTaskbar = False
        form.Deactivate += deactivate
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            rounded = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(int(form.Handle.ToInt64()), 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
        except OSError:
            pass
    if form.InvokeRequired:
        form.Invoke(Action(configure))
    else:
        configure()


class PanelAPI:
    """Only the fixed tray actions are exposed to the local page."""
    def __init__(self, panel):
        self._panel = panel

    def panel_ready(self):
        return self._panel.panel_ready()

    def panel_state(self):
        return self._panel.panel_state()

    def resize_panel(self, width, height):
        return self._panel.resize_panel(width, height)

    def dismiss(self):
        self._panel.dismiss()

    def open_main(self):
        return self._panel.open_main()

    def exit_app(self):
        return self._panel.exit_app()


class TrayPanel:
    def __init__(self, create_window, ui_url, data_dir, show_main):
        self.create_window, self.ui_url = create_window, ui_url
        self.data_dir, self.show_main = Path(data_dir), show_main
        self.window = None
        self.visible = False
        self.error = ""
        self._guard = threading.RLock()
        self._loaded = threading.Event()
        self._closed = threading.Event()
        self._creating = False
        self._configured = False
        self._anchor = None
        self._size = (344, 270)
        self._api = PanelAPI(self)
        self._bridge_ready = False

    def show(self):
        anchor = popup_anchor()
        with self._guard:
            if self._closed.is_set():
                return
            self._anchor, self.visible = anchor, True
            create = self.window is None and not self._creating
            if create:
                self._creating = True
        if create:
            try:
                window = self.create_window("Codex 请求优化器", self.ui_url + "tray.html", width=self._size[0],
                    height=self._size[1], min_size=(240, 120), resizable=False, frameless=True, easy_drag=False,
                    hidden=True, on_top=True, shadow=True, text_select=False, background_color="#ffffff", js_api=self._api)
                window.events.loaded += self._on_loaded
                window.events.closing += self._on_closing
                window.events.closed += self._on_closed
                with self._guard:
                    self.window, self._creating = window, False
                if self._closed.is_set():
                    window.destroy()
                elif self._bridge_ready:
                    self._on_loaded()
            except Exception as exc:
                with self._guard:
                    self._creating, self.visible = False, False
                    self.error = type(exc).__name__
                self.show_main()
                return
        if self._loaded.is_set():
            self._present()

    def _on_closed(self):
        self._closed.set()
        self.visible = False

    def _on_closing(self):
        if self._closed.is_set():
            return True
        self._deactivate()
        return False

    def _on_loaded(self):
        if self._closed.is_set():
            return
        window = self.window
        if window is None:
            return
        with self._guard:
            if not self._configured:
                configure_panel(window, self._deactivate)
                resize_panel_window(window, *self._size)
                self._configured = True
        self._loaded.set()
        if self.visible:
            self._present()
        else:
            self._notify(False)

    def _present(self):
        window = self.window
        if self._closed.is_set() or not self.visible or window is None:
            return
        place_panel(window, self._anchor)
        window.show()
        self._notify(True)

    def _notify(self, visible):
        try:
            self.window.evaluate_js("window.trayPanel && window.trayPanel.setVisible(" + ("true" if visible else "false") + "); true")
        except Exception:
            pass  # The next loaded/bridge event restores state after a resource reload.

    def _deactivate(self, *_):
        if self.visible:
            threading.Thread(target=self.dismiss, name="tray-panel-dismiss", daemon=True).start()

    def panel_ready(self):
        self._bridge_ready = True
        self._on_loaded()
        return self.panel_state()

    def panel_state(self):
        return {"visible": self.visible and not self._closed.is_set()}

    def resize_panel(self, width, height):
        if not all(type(value) in (int, float) and math.isfinite(value) for value in (width, height)):
            return False
        size = max(240, min(600, math.ceil(width))), max(120, min(800, math.ceil(height)))
        with self._guard:
            if size == self._size:
                return True
            if self.window and self._configured and not self._closed.is_set():
                resize_panel_window(self.window, *size)
                if self.visible:
                    place_panel(self.window, self._anchor)
            self._size = size
        return True

    def dismiss(self):
        self.visible = False
        if self.window and not self._closed.is_set():
            self._notify(False)
            self.window.hide()

    def open_main(self):
        self.dismiss()
        self.show_main()
        return {"ok": True}

    def exit_app(self):
        from .exit_flow import request_exit
        self.dismiss()
        try:
            request_exit(self.data_dir, self.show_main)
            return {"ok": True}
        except (OSError, ValueError):
            self.visible = True
            self._present()
            return {"ok": False}

    def destroy(self):
        already_closed = self._closed.is_set()
        self._closed.set()
        self.visible = False
        if self.window and not already_closed:
            self.window.destroy()
