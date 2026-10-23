"""Small Win32 tray entry owned by the independent forwarding service."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading

from .app_icon import APP_ICON


class NativeTray:
    def __init__(self, open_window, exit_app):
        self.open_window, self.exit_app = open_window, exit_app
        self.ready = threading.Event()
        self.error = None
        self.hwnd = None
        self.thread = None
        self.owned_icon = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="inspector-tray", daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError("托盘入口启动失败：" + str(self.error or "timeout"))
        return self

    def stop(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x10, 0, 0)  # WM_CLOSE
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def _run(self):
        try:
            self._message_loop()
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
            self.ready.set()

    def _message_loop(self):
        user = self.user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        result_type = ctypes.c_ssize_t
        callback_type = ctypes.WINFUNCTYPE(result_type, wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                        ("lpfnWndProc", callback_type), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        class Guid(ctypes.Structure):
            _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                        ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]

        class NotifyIcon(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", Guid),
                        ("hBalloonIcon", wintypes.HICON)]

        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        user.RegisterClassExW.argtypes = [ctypes.POINTER(WindowClass)]
        user.RegisterClassExW.restype = wintypes.ATOM
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                       wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.DefWindowProcW.restype = result_type
        user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user.LoadIconW.restype = wintypes.HICON
        user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.LoadImageW.restype = wintypes.HANDLE
        user.GetSystemMetrics.argtypes = [ctypes.c_int]
        user.GetSystemMetrics.restype = ctypes.c_int
        user.DestroyIcon.argtypes = [wintypes.HICON]
        user.DestroyIcon.restype = wintypes.BOOL
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.GetMessageW.restype = wintypes.BOOL
        user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.restype = result_type
        user.CreatePopupMenu.restype = wintypes.HMENU
        user.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user.TrackPopupMenu.restype = wintypes.UINT
        user.DestroyMenu.argtypes = [wintypes.HMENU]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIcon)]
        shell.Shell_NotifyIconW.restype = wintypes.BOOL

        callback_message = 0x8001
        taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")

        def invoke(callback):
            threading.Thread(target=callback, daemon=True).start()

        def window_proc(hwnd, message, wparam, lparam):
            if message == callback_message:
                if lparam in (0x202, 0x203):  # left button up/double click
                    invoke(self.open_window)
                elif lparam == 0x205:  # right button up
                    point = wintypes.POINT()
                    user.GetCursorPos(ctypes.byref(point))
                    menu = user.CreatePopupMenu()
                    user.AppendMenuW(menu, 0x3, 0, "中转服务运行中")
                    user.AppendMenuW(menu, 0x800, 0, None)
                    user.AppendMenuW(menu, 0, 1, "打开主窗口")
                    user.AppendMenuW(menu, 0x800, 0, None)
                    user.AppendMenuW(menu, 0, 2, "退出程序")
                    user.SetForegroundWindow(hwnd)
                    selection = user.TrackPopupMenu(menu, 0x102, point.x, point.y, 0, hwnd, None)
                    user.DestroyMenu(menu)
                    user.PostMessageW(hwnd, 0, 0, 0)
                    if selection == 1:
                        invoke(self.open_window)
                    elif selection == 2:
                        invoke(self.exit_app)
                return 0
            if message == taskbar_created:
                shell.Shell_NotifyIconW(0, ctypes.byref(self.icon))
                return 0
            if message == 0x10:  # WM_CLOSE
                user.DestroyWindow(hwnd)
                return 0
            if message == 2:  # WM_DESTROY
                shell.Shell_NotifyIconW(2, ctypes.byref(self.icon))
                if self.owned_icon:
                    user.DestroyIcon(self.owned_icon)
                    self.owned_icon = None
                user.PostQuitMessage(0)
                return 0
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        self.callback = callback_type(window_proc)
        instance = kernel.GetModuleHandleW(None)
        class_name = "CodexPacketInspectorTray-" + str(os.getpid())
        window_class = WindowClass()
        window_class.cbSize = ctypes.sizeof(WindowClass)
        window_class.lpfnWndProc = self.callback
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        if not user.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = user.CreateWindowExW(0, class_name, "Codex Request Optimizer", 0,
                                        0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.icon = NotifyIcon()
        self.icon.cbSize = ctypes.sizeof(NotifyIcon)
        self.icon.hWnd, self.icon.uID = self.hwnd, 1
        self.icon.uFlags, self.icon.uCallbackMessage = 7, callback_message
        self.owned_icon = user.LoadImageW(None, str(APP_ICON), 1,
                                         user.GetSystemMetrics(49), user.GetSystemMetrics(50), 0x10)
        self.icon.hIcon = self.owned_icon or user.LoadIconW(None, ctypes.c_void_p(32512))
        self.icon.szTip = "Codex 请求优化器 · 中转服务运行中"
        if not shell.Shell_NotifyIconW(0, ctypes.byref(self.icon)):
            user.DestroyWindow(self.hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        self.ready.set()
        message = wintypes.MSG()
        while True:
            status = user.GetMessageW(ctypes.byref(message), None, 0, 0)
            if status == 0:
                break
            if status == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        self.hwnd = None
        user.UnregisterClassW(class_name, instance)
