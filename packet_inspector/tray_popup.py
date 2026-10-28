"""Tray popup display and dismissal driven by explicit user input."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


OVERFLOW_CLASSES = {"NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}


def _user32():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.WindowFromPoint.argtypes, user.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
    user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsWindowVisible.argtypes, user.IsWindowVisible.restype = [wintypes.HWND], wintypes.BOOL
    user.GetAsyncKeyState.argtypes, user.GetAsyncKeyState.restype = [ctypes.c_int], ctypes.c_short
    return user


class PopupInteraction:
    def __init__(self, user, *, preserve_shell_focus=False):
        self.user = user
        self.preserve_shell_focus = preserve_shell_focus
        self.buttons = self._buttons()
        self.cancel_key = self._cancel_key()

    def _buttons(self):
        return sum(1 << index for index, key in enumerate((0x01, 0x02, 0x04))
                   if self.user.GetAsyncKeyState(key) & 0x8000)

    def _cancel_key(self):
        def down(key):
            return bool(self.user.GetAsyncKeyState(key) & 0x8000)

        return (any(down(key) for key in (0x1B, 0x5B, 0x5C))  # Escape or Windows key
                or down(0x12) and (down(0x09) or down(0x73)))  # Alt+Tab / Alt+F4

    def should_dismiss(self, window):
        user = self.user
        handle = int(window.native.Handle.ToInt64())
        cancel_key = self._cancel_key()
        pressed = self._buttons()
        new_press = pressed & ~self.buttons
        cancel = cancel_key and not self.cancel_key
        self.buttons, self.cancel_key = pressed, cancel_key
        if new_press:
            point, rect = wintypes.POINT(), wintypes.RECT()
            if not user.GetCursorPos(ctypes.byref(point)) or not user.GetWindowRect(handle, ctypes.byref(rect)):
                return bool(cancel)
            cancel |= not (rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom)
        return bool(cancel)


def popup_interaction_at(anchor):
    if os.name != "nt":
        return None
    user = _user32()
    hit = user.WindowFromPoint(wintypes.POINT(*anchor[0]))
    foreground = user.GetForegroundWindow()
    # The desktop may receive the tray event after the pointer has left the icon.
    for candidate in (hit, foreground):
        root = user.GetAncestor(candidate, 2) if candidate else 0
        name = ctypes.create_unicode_buffer(128)
        if root and user.IsWindowVisible(root) and user.GetClassNameW(root, name, len(name)):
            if name.value in OVERFLOW_CLASSES:
                return PopupInteraction(user, preserve_shell_focus=True)
    # Normal tray menus also use explicit cancellation. Losing activation alone
    # can be caused by WebView reloads or shell updates while the user is idle.
    return PopupInteraction(user)


def show_panel_window(window, inactive=False):
    if os.name != "nt":
        window.show()
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowLongPtrW.argtypes, user.GetWindowLongPtrW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    from System import Action
    form = window.native

    def show():
        handle = int(form.Handle.ToInt64())
        style = user.GetWindowLongPtrW(handle, -20)
        # Only our own popup changes style. Explorer's windows are read-only.
        desired = style | 0x08000000 if inactive else style & ~0x08000000  # WS_EX_NOACTIVATE
        if desired != style:
            ctypes.set_last_error(0)
            if not user.SetWindowLongPtrW(handle, -20, desired) and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        if not inactive:
            form.Show()
            form.Activate()
        # A preloaded topmost window can still be behind Explorer's topmost
        # overflow panel. Reinsert our menu at the top each time it is shown,
        # without activating it or changing its owner's order.
        flags = 0x01 | 0x02 | 0x10 | 0x40 | 0x0200  # NOSIZE/NOMOVE/NOACTIVATE/SHOWWINDOW/NOOWNERZORDER
        if not user.SetWindowPos(handle, -1, 0, 0, 0, 0, flags):  # HWND_TOPMOST
            raise ctypes.WinError(ctypes.get_last_error())

    if form.InvokeRequired:
        form.Invoke(Action(show))
    else:
        show()
