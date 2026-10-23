"""Suppress Windows attention highlighting for this application's window only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


HSHELL_FLASH = 0x8006
WM_NCDESTROY = 0x82


class FlashWindowInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD)]


class TaskbarAttention:
    def __init__(self, window):
        self.window = window
        self.hwnd = None
        self.callback = None
        self.suppressed = 0
        window.events.before_show += self.install

    def clear(self):
        if self.hwnd:
            # FLASHW_STOP clears attention without activating or raising the UI.
            info = FlashWindowInfo(ctypes.sizeof(FlashWindowInfo), self.hwnd, 0, 0, 0)
            self.user.FlashWindowEx(ctypes.byref(info))

    def _message(self, hwnd, message, wparam, lparam, subclass_id, _data):
        if message == self.shell_message and wparam == HSHELL_FLASH and lparam == self.hwnd:
            self.clear()
            self.suppressed += 1
            return 0
        if message == WM_NCDESTROY:
            self.user.DeregisterShellHookWindow(hwnd)
            self.common.RemoveWindowSubclass(hwnd, self.callback, subclass_id)
            self.hwnd = None
        return self.common.DefSubclassProc(hwnd, message, wparam, lparam)

    def install(self):
        if os.name != "nt" or self.hwnd:
            return
        # before_show runs on the native UI thread, as required by SetWindowSubclass.
        user = self.user = ctypes.WinDLL("user32", use_last_error=True)
        common = self.common = ctypes.WinDLL("comctl32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM,
                                          ctypes.c_size_t, ctypes.c_size_t)
        user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user.RegisterWindowMessageW.restype = wintypes.UINT
        user.RegisterShellHookWindow.argtypes = [wintypes.HWND]
        user.RegisterShellHookWindow.restype = wintypes.BOOL
        user.DeregisterShellHookWindow.argtypes = [wintypes.HWND]
        user.DeregisterShellHookWindow.restype = wintypes.BOOL
        user.FlashWindowEx.argtypes = [ctypes.POINTER(FlashWindowInfo)]
        user.FlashWindowEx.restype = wintypes.BOOL
        common.SetWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t, ctypes.c_size_t]
        common.SetWindowSubclass.restype = wintypes.BOOL
        common.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]
        common.RemoveWindowSubclass.restype = wintypes.BOOL
        common.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        common.DefSubclassProc.restype = ctypes.c_ssize_t
        self.shell_message = user.RegisterWindowMessageW("SHELLHOOK")
        if not self.shell_message:
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = int(self.window.native.Handle.ToInt64())
        self.callback = callback_type(self._message)
        if not common.SetWindowSubclass(hwnd, self.callback, 1, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not user.RegisterShellHookWindow(hwnd):
            error = ctypes.get_last_error()
            common.RemoveWindowSubclass(hwnd, self.callback, 1)
            raise ctypes.WinError(error)
        self.hwnd = hwnd
        self.clear()
