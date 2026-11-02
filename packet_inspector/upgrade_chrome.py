"""White Tk title bar with native Windows taskbar, minimize and drag behavior."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
import tkinter as tk


@lru_cache(maxsize=1)
def _native():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user.GetWindowLongW.argtypes, user.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
    user.SetWindowLongW.argtypes, user.SetWindowLongW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
    for name in ("GetWindowRect", "GetClientRect"):
        function = getattr(user, name)
        function.argtypes, function.restype = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    user.ReleaseCapture.argtypes, user.ReleaseCapture.restype = [], wintypes.BOOL
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.ShowWindow.argtypes, user.ShowWindow.restype = [wintypes.HWND, ctypes.c_int], wintypes.BOOL
    return user


class _CaptionButton(tk.Canvas):
    def __init__(self, parent, scale, kind, command):
        self.u, self.kind, self.command = scale, kind, command
        self.enabled, self.hovered = True, False
        super().__init__(parent, width=scale(46), height=scale(48), bg="#ffffff", highlightthickness=0,
                         borderwidth=0, takefocus=1, cursor="arrow")
        for event in ("<Button-1>", "<Return>", "<space>"):
            self.bind(event, lambda _event: self.invoke())
        for event in ("<Enter>", "<FocusIn>"):
            self.bind(event, lambda _event: self.hover(True))
        for event in ("<Leave>", "<FocusOut>"):
            self.bind(event, lambda _event: self.hover(False))
        self.draw()

    def invoke(self):
        if self.enabled:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(takefocus=int(enabled))
        self.draw()

    def hover(self, active):
        self.hovered = active
        self.draw()

    def draw(self):
        active = self.enabled and self.hovered
        self.configure(bg="#c42b1c" if active and self.kind == "close" else "#e8edf5" if active else "#ffffff")
        ink = "#ffffff" if active and self.kind == "close" else "#3e4858" if self.enabled else "#a0a9b7"
        self.delete("all")
        x, y, radius = self.u(23), self.u(24), self.u(5)
        if self.kind == "close":
            self.create_line(x-radius, y-radius, x+radius, y+radius, fill=ink, width=self.u(1))
            self.create_line(x-radius, y+radius, x+radius, y-radius, fill=ink, width=self.u(1))
        else:
            self.create_line(x-radius, y, x+radius, y, fill=ink, width=self.u(1))


class UpgradeChrome:
    def __init__(self, window, scale, font, title, on_close):
        self.window, self.on_close = window, on_close
        self._fitting = False
        if os.name == "nt":
            # Let Tk measure a borderless client area itself. Editing its
            # caption style alone leaves stale non-client frame dimensions.
            window.overrideredirect(True)
        self.frame = tk.Frame(window, bg="#ffffff", height=scale(48))
        if os.name == "nt":
            self.frame.pack(side="top", fill="x")
        self.frame.pack_propagate(False)
        self.title_label = tk.Label(self.frame, text=title, bg="#ffffff", fg="#1e293b", font=(font, 12, "bold"), padx=scale(14), anchor="w")
        self.title_label.pack(side="left", fill="both", expand=True, anchor="w")
        self.close_button = _CaptionButton(self.frame, scale, "close", lambda: self.on_close())
        self.close_button.pack(side="right")
        self.close_button.set_enabled(False)
        self.minimize_button = _CaptionButton(self.frame, scale, "minimize", lambda: self.minimize())
        self.minimize_button.pack(side="right")
        for node in (self.frame, self.title_label):
            node.bind("<ButtonPress-1>", self.begin_drag)
        window.bind("<Map>", self._mapped)

    def _handle(self):
        return _native().GetAncestor(self.window.winfo_id(), 2)

    def _install(self):
        user, handle = _native(), self._handle()
        style, extended = user.GetWindowLongW(handle, -16), user.GetWindowLongW(handle, -20)
        # WS_EX_APPWINDOW keeps this a normal taskbar/Alt+Tab window; retain
        # system close/minimize commands without restoring the native caption.
        changes = ((-16, style, (style & ~0x00C00000) | 0x000A0000),
                   (-20, extended, (extended & ~0x00000080) | 0x00040000))
        modified = []
        try:
            for index, previous, value in changes:
                if previous == value:
                    continue
                ctypes.set_last_error(0)
                result = user.SetWindowLongW(handle, index, value)
                if not result and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                modified.append((index, previous))
            if modified and not user.SetWindowPos(handle, None, 0, 0, 0, 0, 0x20 | 0x01 | 0x02 | 0x04 | 0x10):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            for index, previous in reversed(modified):
                user.SetWindowLongW(handle, index, previous)
            raise
        return handle

    def fit(self, width, height):
        if self._fitting:
            return
        self._fitting = True
        try:
            self.window.geometry(f"{width}x{height}")
            self.window.update_idletasks()
            if os.name == "nt":
                self._install()
        finally:
            self._fitting = False

    def _mapped(self, event):
        if event.widget is self.window:
            self.window.after_idle(self.refresh)

    def refresh(self):
        if os.name == "nt" and self.window.winfo_exists():
            self._install()

    def minimize(self):
        if os.name == "nt":
            _native().ShowWindow(self._handle(), 6)  # SW_MINIMIZE also works for a borderless Tk window.
        else:
            self.window.iconify()

    def begin_drag(self, _event):
        if os.name == "nt":
            user, handle = _native(), self._handle()
            user.ReleaseCapture()
            user.SendMessageW(handle, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN / HTCAPTION
