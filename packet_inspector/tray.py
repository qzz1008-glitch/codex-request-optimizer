"""Small Win32 tray entry owned by the independent forwarding service."""
from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
import os
import threading

from .app_icon import APP_ICON

STATUS_MESSAGE = 0x8002
NATIVE_MENU_MESSAGE = 0x8003
ICON_REFRESH_TIMER = 1


def tray_status_lines(status):
    """Four read-only menu rows; unknown values stay unknown."""
    english = status.get("language") == "en"
    connection = {"connected": "Connected" if english else "已接入",
                  "disconnected": "Disconnected" if english else "未接入"}.get(
                      status.get("connection_state"), "Connection unknown" if english else "接入状态未知")
    relay = {"running": "Relay running" if english else "中转正常",
             "switching": "Relay switching" if english else "中转切换中"}.get(status.get("relay_state"))
    header = connection + " · " + relay if relay else "Status unavailable" if english else "状态暂不可用"
    def count(key):
        value = status.get(key)
        return f"{value:,}" if type(value) is int and value >= 0 else "—"
    average, samples = status.get("average_compact_input_tokens"), status.get("compaction_samples")
    average = (f"{math.floor(average + .5):,}" if type(samples) is int and samples > 0
               and type(average) in (int, float) and math.isfinite(average) and average >= 0 else "—")
    if english:
        return (header, "Recorded requests: " + count("recorded_requests"),
                "Protection triggers: " + count("protected_requests"), "Average context: " + average + " Token")
    return (header, "记录请求：" + count("recorded_requests") + " 个",
            "触发保护：" + count("protected_requests") + " 次", "平均上下文：" + average + " Token")


def close_entry_tray(pid, executable):
    """Close only our old entry's tray thread; its HTTP listener keeps running."""
    if os.name != "nt" or type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user.FindWindowW("CodexPacketInspectorTray-" + str(pid), None)
    if not hwnd:
        return False
    owner = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    if owner.value != pid:
        return False
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        from pathlib import Path
        path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            return False
        if Path(path.value).resolve() != Path(executable).resolve():
            return False
    finally:
        kernel.CloseHandle(process)
    return bool(user.PostMessageW(hwnd, 0x10, 0, 0))  # WM_CLOSE, never a process stop.


class NativeTray:
    def __init__(self, open_window, exit_app, *, status_reader=None, show_panel=None):
        self.open_window, self.exit_app = open_window, exit_app
        self.status_reader = status_reader
        self.show_panel = show_panel
        self.ready = threading.Event()
        self.error = None
        self.hwnd = None
        self.thread = None
        self.owned_icon = None
        self._icon_size = None
        self._scaled_icon_loader = None
        self.status_thread = None
        self._status = {}
        self._status_guard = threading.Lock()
        self._status_requested = threading.Event()
        self._stopped = threading.Event()
        self._menu_open = False
        self._menu = None
        self._menu_text = {}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="inspector-tray", daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError("托盘入口启动失败：" + str(self.error or "timeout"))
        if self.status_reader is not None:
            self._status_requested.set()
            self.status_thread = threading.Thread(target=self._status_loop, name="tray-status", daemon=True)
            self.status_thread.start()
        return self

    def stop(self):
        self._stopped.set()
        self._status_requested.set()
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x10, 0, 0)  # WM_CLOSE
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        if self.status_thread and self.status_thread is not threading.current_thread():
            self.status_thread.join(timeout=3)

    def update_status(self, status):
        with self._status_guard:
            self._status = dict(status)
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, STATUS_MESSAGE, 0, 0)

    def _status_loop(self):
        while not self._stopped.is_set():
            requested = self._status_requested.wait(1 if self._menu_open else None)
            self._status_requested.clear()
            if self._stopped.is_set():
                break
            if not requested and not self._menu_open:
                continue
            try:
                status = self.status_reader()
                if not isinstance(status, dict):
                    raise ValueError("invalid tray status")
            except Exception:
                with self._status_guard:
                    status = {"language": self._status.get("language")}
            self.update_status(status)

    def menu_items(self):
        with self._status_guard:
            status = dict(self._status)
        header, recorded, protected, context = tray_status_lines(status)
        english = status.get("language") == "en"
        return [(100, header, 0x3), (0, None, 0x800),
                (101, recorded, 0x3), (102, protected, 0x3), (103, context, 0x3), (0, None, 0x800),
                (1, "Open main window" if english else "打开主窗口", 0), (0, None, 0x800),
                (2, "Exit" if english else "退出程序", 0)]

    def _refresh_menu(self):
        items = self.menu_items()
        if self._menu:
            changed = False
            for identity, text, flags in items:
                if identity and self._menu_text.get(identity) != text:
                    self.user.ModifyMenuW(self._menu, identity, flags, identity, text)
                    self._menu_text[identity] = text
                    changed = True
            if changed:
                self.user.DrawMenuBar(self.hwnd)
        with self._status_guard:
            english = self._status.get("language") == "en"
        tip = ("Codex Request Optimizer" if english else "Codex 请求优化器") + " · " + items[0][1]
        if self.icon.szTip != tip[:127]:
            self.icon.szTip = tip[:127]
            self.shell.Shell_NotifyIconW(1, ctypes.byref(self.icon))

    def _icon_dimensions(self):
        try:
            taskbar = self.user.FindWindowW("Shell_TrayWnd", None)
            dpi = self.user.GetDpiForWindow(taskbar or self.hwnd)
            if dpi:
                size = tuple(self.user.GetSystemMetricsForDpi(index, dpi) for index in (49, 50))
                if all(value > 0 for value in size):
                    return size
        except (AttributeError, OSError):
            pass
        return tuple(self.user.GetSystemMetrics(index) or 16 for index in (49, 50))

    def _load_icon(self, size):
        if self._scaled_icon_loader is not None:
            handle = wintypes.HICON()
            result = self._scaled_icon_loader(None, str(APP_ICON), *size, ctypes.byref(handle))
            if result >= 0 and handle.value:
                return handle.value
            if handle.value:
                self.user.DestroyIcon(handle.value)
        return self.user.LoadImageW(None, str(APP_ICON), 1, *size, 0x10)

    def _refresh_icon(self, *, add=False):
        if not self.hwnd or getattr(self, "icon", None) is None:
            return False
        size = self._icon_dimensions()
        if not add and size == self._icon_size:
            return True
        previous, owned = self.icon.hIcon, self.owned_icon
        loaded = self._load_icon(size)
        if not loaded and previous and not add:
            return False  # Keep the current product icon when a reload fails.
        self.icon.hIcon = loaded or previous or self.user.LoadIconW(None, ctypes.c_void_p(32512))
        if not self.shell.Shell_NotifyIconW(0 if add else 1, ctypes.byref(self.icon)):
            self.icon.hIcon = previous
            if loaded:
                self.user.DestroyIcon(loaded)
            return False
        if loaded:
            self.owned_icon, self._icon_size = loaded, size
            if owned and owned != loaded:
                self.user.DestroyIcon(owned)
        return True

    def _launch_panel(self):
        try:
            if self.show_panel() is not False:
                return
        except Exception:
            pass
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, NATIVE_MENU_MESSAGE, 0, 0)

    def _show_native_menu(self):
        user, hwnd, point = self.user, self.hwnd, wintypes.POINT()
        user.GetCursorPos(ctypes.byref(point))
        menu = user.CreatePopupMenu()
        self._menu, self._menu_text = menu, {}
        for identity, text, flags in self.menu_items():
            user.AppendMenuW(menu, flags, identity, text)
            if identity:
                self._menu_text[identity] = text
        user.SetForegroundWindow(hwnd)
        self._menu_open = True
        if self.status_reader is not None:
            self._status_requested.set()
        try:
            selection = user.TrackPopupMenu(menu, 0x102, point.x, point.y, 0, hwnd, None)
        finally:
            self._menu_open, self._menu, self._menu_text = False, None, {}
            user.DestroyMenu(menu)
        user.PostMessageW(hwnd, 0, 0, 0)
        if selection in (1, 2):
            callback = self.open_window if selection == 1 else self.exit_app
            threading.Thread(target=callback, daemon=True).start()

    def _run(self):
        try:
            self._message_loop()
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
            self.ready.set()

    def _message_loop(self):
        user = self.user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        shell = self.shell = ctypes.WinDLL("shell32", use_last_error=True)
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
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        for name, arguments, result in (
            ("GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
            ("GetSystemMetricsForDpi", [ctypes.c_int, wintypes.UINT], ctypes.c_int),
            ("SetThreadDpiAwarenessContext", [wintypes.HANDLE], wintypes.HANDLE),
        ):
            function = getattr(user, name, None)
            if function is not None:
                function.argtypes, function.restype = arguments, result
        set_thread_dpi = getattr(user, "SetThreadDpiAwarenessContext", None)
        if set_thread_dpi is not None:
            # Scope DPI awareness to the tray thread, leaving WebView windows alone.
            if not set_thread_dpi(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
                set_thread_dpi(ctypes.c_void_p(-3))  # Earlier per-monitor support.
        try:
            self.icon_library = ctypes.WinDLL("comctl32", use_last_error=True)
            self._scaled_icon_loader = self.icon_library.LoadIconWithScaleDown
            self._scaled_icon_loader.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                                 ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HICON)]
            self._scaled_icon_loader.restype = ctypes.c_long
        except (AttributeError, OSError):
            self._scaled_icon_loader = None
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
        user.ModifyMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user.DrawMenuBar.argtypes = [wintypes.HWND]
        user.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user.TrackPopupMenu.restype = wintypes.UINT
        user.DestroyMenu.argtypes = [wintypes.HMENU]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        user.SetTimer.restype = ctypes.c_size_t
        user.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        user.KillTimer.restype = wintypes.BOOL
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIcon)]
        shell.Shell_NotifyIconW.restype = wintypes.BOOL

        callback_message = 0x8001
        taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")

        def invoke(callback):
            threading.Thread(target=callback, daemon=True).start()

        def window_proc(hwnd, message, wparam, lparam):
            if message == callback_message:
                if lparam == 0x203:  # WM_LBUTTONDBLCLK; ignore single left clicks.
                    invoke(self.open_window)
                elif lparam == 0x205:  # right button up
                    if self.show_panel is not None:
                        # A WebView popup opened from overflow must leave Explorer
                        # active; activating this invisible owner closes its flyout.
                        user.AllowSetForegroundWindow(0xFFFFFFFF)
                        invoke(self._launch_panel)
                    else:
                        self._show_native_menu()
                return 0
            if message == STATUS_MESSAGE:
                self._refresh_menu()
                return 0
            if message == NATIVE_MENU_MESSAGE:
                self._show_native_menu()
                return 0
            if message in (0x1A, 0x7E, 0x2E0):  # settings, display, or DPI changed
                self._refresh_icon()
                # Explorer can finish updating its DPI after this broadcast.
                user.SetTimer(hwnd, ICON_REFRESH_TIMER, 250, None)
                return 0
            if message == 0x113 and wparam == ICON_REFRESH_TIMER:  # WM_TIMER
                user.KillTimer(hwnd, ICON_REFRESH_TIMER)
                self._refresh_icon()
                return 0
            if message == taskbar_created:
                self._refresh_icon(add=True)
                return 0
            if message == 0x10:  # WM_CLOSE
                user.DestroyWindow(hwnd)
                return 0
            if message == 2:  # WM_DESTROY
                self._stopped.set()
                self._status_requested.set()
                user.KillTimer(hwnd, ICON_REFRESH_TIMER)
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
        self.icon.szTip = "Codex 请求优化器 · 中转服务运行中"
        if not self._refresh_icon(add=True):
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
