"""Mouse-opened tray menus that leave Explorer's overflow popup active."""
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


class OverflowPopup:
    def __init__(self, user, owner):
        self.user, self.owner = user, owner
        self.foreground = user.GetForegroundWindow()
        self.buttons = self._buttons()
        self.escape = bool(user.GetAsyncKeyState(0x1B) & 0x8000)

    def _buttons(self):
        return sum(1 << index for index, key in enumerate((0x01, 0x02, 0x04))
                   if self.user.GetAsyncKeyState(key) & 0x8000)

    def should_dismiss(self, window):
        user = self.user
        handle = int(window.native.Handle.ToInt64())
        foreground = user.GetForegroundWindow()
        root = user.GetAncestor(foreground, 2) if foreground else 0
        # Clicking a menu action can focus WebView before its click handler runs.
        # Let that action finish rather than hiding it on mouse-down.
        in_menu = foreground == handle or root == handle
        if not in_menu and (foreground not in (self.foreground, self.owner)
                            or not user.IsWindowVisible(self.owner)):
            return True
        escape = bool(user.GetAsyncKeyState(0x1B) & 0x8000)
        pressed = self._buttons()
        new_press = pressed & ~self.buttons
        cancel = escape and not self.escape
        self.buttons, self.escape = pressed, escape
        if new_press:
            point, rect = wintypes.POINT(), wintypes.RECT()
            if not user.GetCursorPos(ctypes.byref(point)) or not user.GetWindowRect(handle, ctypes.byref(rect)):
                return True
            cancel |= not (rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom)
        return bool(cancel)


def overflow_popup_at(anchor):
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
                return OverflowPopup(user, root)
    return None


def show_panel_window(window, inactive=False):
    if os.name != "nt":
        window.show()
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowLongPtrW.argtypes, user.GetWindowLongPtrW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
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
        if inactive:
            user.ShowWindow(handle, 4)  # SW_SHOWNOACTIVATE; do not call Form.Activate.
        else:
            form.Show()
            form.Activate()

    if form.InvokeRequired:
        form.Invoke(Action(show))
    else:
        show()
