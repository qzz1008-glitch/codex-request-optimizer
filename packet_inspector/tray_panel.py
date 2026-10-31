"""A reusable WebView tray panel owned by the existing desktop process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import threading

from .tray_popup import PopupPolicy, popup_interaction_at, show_panel_window


def popup_position(anchor, monitor_bounds, size, policy=None):
    """Keep an edge at the cursor, flipping only to fit on this monitor."""
    left, top, right, bottom = monitor_bounds
    width, height = size
    policy = policy or PopupPolicy()
    x = anchor[0] - width if policy.horizontal == "before" or (
        policy.horizontal == "auto" and anchor[0] + width > right) else anchor[0]
    y = anchor[1] - height if policy.vertical == "before" or (
        policy.vertical == "auto" and anchor[1] + height > bottom) else anchor[1]
    x, y = x + policy.offset_x, y + policy.offset_y
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


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
    # The cursor can be on the taskbar; excluding it would introduce a gap.
    bounds = monitor.monitor
    return (point.x, point.y), (bounds.left, bounds.top, bounds.right, bounds.bottom)


def place_panel(window, anchor, policy=None):
    if os.name != "nt":
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    handle, rect = int(window.native.Handle.ToInt64()), wintypes.RECT()
    if user.GetWindowRect(handle, ctypes.byref(rect)):
        policy = policy or PopupPolicy()
        x, y = popup_position(*anchor, (rect.right - rect.left, rect.bottom - rect.top), policy)
        # Keep the visible popup above the overflow panel when layout changes.
        # No SHOWWINDOW flag: positioning a preloaded popup must keep it hidden.
        user.SetWindowPos(handle, -1 if policy.topmost else -2, x, y, 0, 0, 0x01 | 0x10 | 0x0200)


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

    def configure_window(self, options):
        return self._panel.configure_window(options)

    def resize_panel(self, width, height):
        return self._panel.resize_panel(width, height)

    def dismiss(self):
        self._panel.dismiss()

    def open_main(self):
        return self._panel.open_main()

    def exit_app(self):
        return self._panel.exit_app()

    def restart_service(self):
        return self._panel.restart_service()


class TrayPanel:
    MIN_WIDTH = 192

    def __init__(self, create_window, ui_url, data_dir, show_main, *, ready_changed=None):
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
        self._size = (216, 280)
        self._api = PanelAPI(self)
        self._bridge_ready = False
        self._ready_changed = ready_changed or (lambda ready: None)
        self._policy = PopupPolicy()
        self._notification = threading.Event()
        self._notifier = None
        self._interaction = None
        self._interaction_stop = None
        self._show_generation = 0

    def show(self):
        anchor = popup_anchor()
        interaction = popup_interaction_at(anchor)
        with self._guard:
            if self._closed.is_set():
                return
            self._stop_interaction()
            self._interaction = interaction
            self._show_generation += 1
            self._anchor, self.visible = anchor, True
        self.prepare()
        if self._loaded.is_set():
            self._present()

    def prepare(self):
        """Load the small menu during startup, without showing it or reading statistics."""
        with self._guard:
            if self._closed.is_set():
                return
            create = self.window is None and not self._creating
            if create:
                self._creating = True
        if create:
            try:
                window = self.create_window("Codex 请求优化器", self.ui_url + "tray.html", width=self._size[0],
                    height=self._size[1], min_size=(self.MIN_WIDTH, 120), resizable=False, frameless=True, easy_drag=False,
                    hidden=True, on_top=True, shadow=True, text_select=False, background_color="#ffffff", js_api=self._api)
                window.events.before_load += self._on_loading
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
                    show_fallback = self.visible
                    self._creating, self.visible = False, False
                    self.error = type(exc).__name__
                if show_fallback:
                    self.show_main()
                return

    def _on_closed(self):
        self._closed.set()
        self._ready_changed(False)
        self.visible = False
        self._stop_interaction()
        self._notification.set()

    def _on_closing(self):
        if self._closed.is_set():
            return True
        self._request_dismiss()
        return False

    def _on_loading(self):
        # pywebview raises this synchronously on its UI thread. Never wait for
        # our guard or invoke the UI here: a bridge call may already hold it.
        self._bridge_ready = False
        self._loaded.clear()
        self._ready_changed(False)

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
            if not self._bridge_ready or self._loaded.is_set():
                return
            # Native navigation completion is not page readiness. The page
            # acknowledges its CSS/fonts, measured layout and bridge first.
            self._loaded.set()
            self._ready_changed(True)
        if self.visible:
            self._present()
        else:
            self._notify()

    def _present(self):
        with self._guard:
            window = self.window
            if self._closed.is_set() or not self.visible or window is None or not self._loaded.is_set():
                return
            policy = self._policy
            place_panel(window, self._anchor, policy)
            inactive = not policy.activate or (policy.preserve_overflow_focus and self._interaction is not None
                                               and self._interaction.preserve_shell_focus)
            show_panel_window(window, inactive=inactive, topmost=policy.topmost)
            self._watch_interaction()
            self._notify()

    def _stop_interaction(self):
        if self._interaction_stop:
            self._interaction_stop.set()
            self._interaction_stop = None

    def _watch_interaction(self):
        with self._guard:
            if not self._interaction or self._interaction_stop or not self.visible or self._closed.is_set():
                return
            stop = self._interaction_stop = threading.Event()
            interaction, window = self._interaction, self.window

        def watch():
            while not stop.wait(.05):
                try:
                    policy = self._policy
                    dismiss = interaction.should_dismiss(window, outside=policy.dismiss_outside,
                                                         escape=policy.dismiss_escape, switch=policy.dismiss_switch)
                except (OSError, ValueError):
                    continue  # A failed observation is not a user dismissal.
                if dismiss:
                    with self._guard:
                        if self._interaction_stop is stop:
                            self.dismiss()
                    return

        threading.Thread(target=watch, name="tray-popup-interaction", daemon=True).start()

    def _notify(self):
        with self._guard:
            if self._notifier is None and not self._closed.is_set():
                self._notifier = threading.Thread(target=self._notification_loop, name="tray-panel-state", daemon=True)
                self._notifier.start()
        self._notification.set()

    def _notification_loop(self):
        while True:
            self._notification.wait()
            self._notification.clear()
            if self._closed.is_set():
                return
            try:
                # The page asks for the current state, so a late notification
                # cannot restore visibility from before a newer dismiss/show.
                self.window.evaluate_js("window.trayPanel && window.trayPanel.sync(); true")
            except Exception:
                pass  # A loaded/bridge event retries after a resource reload.

    def _deactivate(self, *_):
        if self._interaction is None:
            self._request_dismiss()

    def _request_dismiss(self):
        if self.visible:
            generation = self._show_generation

            def dismiss():
                with self._guard:
                    if self.visible and not self._closed.is_set() and generation == self._show_generation:
                        self.dismiss()

            threading.Thread(target=dismiss, name="tray-panel-dismiss", daemon=True).start()

    def panel_ready(self):
        self._bridge_ready = True
        self._on_loaded()
        return self.panel_state()

    def configure_window(self, options):
        try:
            policy = self._policy.updated(options)
        except ValueError:
            return False
        with self._guard:
            if self._closed.is_set():
                return False
            self._policy = policy
        return True

    def panel_state(self):
        return {"visible": self.visible and not self._closed.is_set(), "min_width": self.MIN_WIDTH}

    def resize_panel(self, width, height):
        if not all(type(value) in (int, float) and math.isfinite(value) for value in (width, height)):
            return False
        size = max(self.MIN_WIDTH, min(600, math.ceil(width))), max(120, min(800, math.ceil(height)))
        with self._guard:
            if size == self._size:
                return True
            if self.window and self._configured and not self._closed.is_set():
                resize_panel_window(self.window, *size)
                if self.visible and self._loaded.is_set():
                    place_panel(self.window, self._anchor, self._policy)
            self._size = size
        return True

    def dismiss(self):
        with self._guard:
            self.visible = False
            self._show_generation += 1
            self._stop_interaction()
            self._interaction = None
            if self.window and not self._closed.is_set():
                self.window.hide()
                self._notify()

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

    def restart_service(self):
        from .service_restart import restart_from_tray
        self.dismiss()
        return restart_from_tray(self.data_dir)

    def destroy(self):
        already_closed = self._closed.is_set()
        self._closed.set()
        self._ready_changed(False)
        self.visible = False
        self._stop_interaction()
        self._notification.set()
        if self.window and not already_closed:
            self.window.destroy()
