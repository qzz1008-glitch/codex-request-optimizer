"""Bind the native subclass API used by the desktop window."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


WM_NCDESTROY = 0x82


def window_subclass_api():
    common = ctypes.WinDLL("comctl32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM,
                                      ctypes.c_size_t, ctypes.c_size_t)
    common.SetWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t, ctypes.c_size_t]
    common.SetWindowSubclass.restype = wintypes.BOOL
    common.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]
    common.RemoveWindowSubclass.restype = wintypes.BOOL
    common.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    common.DefSubclassProc.restype = ctypes.c_ssize_t
    return common, callback_type
