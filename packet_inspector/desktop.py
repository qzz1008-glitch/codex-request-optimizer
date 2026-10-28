"""Windows WebView2 window; the forwarding service runs in a separate process."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, ProxyHandler, build_opener

from .server import argument_parser, default_data_dir
from .desktop_ui import DesktopUI, window_geometry
from .app_icon import APP_ICON
from .frontend_dev import load_profile, validate_ui_dir, resolve_profile, remember_profile, installed_profile
from .window_lifecycle import WindowLifecycle, window_control_prefix
from .window_chrome import WindowChrome


def app_command():
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "packet_inspector"]


def service_arguments(options):
    args = ["--port", str(options.port), "--data-dir", str(options.data_dir.resolve()),
            "--config", str(options.config.resolve())]
    if options.upstream:
        args += ["--upstream", options.upstream]
    return args


def local_api(port, path, data=None, timeout=2):
    request = Request(f"http://127.0.0.1:{port}" + path,
                      data=None if data is None else json.dumps(data).encode(),
                      headers={} if data is None else {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def start_process(arguments):
    # A UI or background service can outlive its launcher. Give every frozen
    # instance its own extraction directory instead of sharing the launcher's.
    return subprocess.Popen(app_command() + arguments,
                            cwd=None if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parent.parent),
                            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ensure_service(options):
    child = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            health = local_api(options.port, "/health")
            if health.get("app") != "codex-packet-inspector":
                raise RuntimeError("此端口被其他程序占用，请先释放端口。")
            status = local_api(options.port, "/api/status")
            if Path(status["data_dir"]).resolve() != options.data_dir.resolve():
                raise RuntimeError("此端口正在使用另一个数据目录。")
            if Path(status["profile"]["config_path"]).resolve() != options.config.resolve():
                raise RuntimeError("此端口正在使用另一份 Codex 配置。")
            if (status.get("provider_routing_supported") and status.get("auto_detect_providers")
                    and status.get("profile", {}).get("active")):
                local_api(options.port, "/api/providers/sync", {}, timeout=10)
                status = local_api(options.port, "/api/status")
            return status
        except (URLError, TimeoutError, ConnectionError, OSError):
            if child is None:
                child = start_process(["--background", "--tray"] + service_arguments(options))
            elif child.poll() is not None:
                # A concurrent launcher may have started the same service.
                time.sleep(.2)
            time.sleep(.15)
    raise RuntimeError("后台服务未能启动，请查看数据目录中的 startup-error.txt。")


def tray_available(data_dir, status):
    if status.get("desktop_tray"):
        return True
    try:
        runtime = json.loads((Path(data_dir) / "runtime.json").read_text(encoding="utf8"))
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        return bool(user.FindWindowW("CodexPacketInspectorTray-" + str(int(runtime["pid"])), None))
    except (OSError, ValueError, KeyError, TypeError):
        return False


class SingleWindow:
    """One UI per data directory across builds, modes, and entry ports."""

    def __init__(self, data_dir):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        self.kernel.WaitForMultipleObjects.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # Editing the entry port or switching EXEs must not open another UI for
        # the same settings and records. A repeated launch only raises its window.
        prefix = window_control_prefix(data_dir)
        self.event = self.kernel.CreateEventW(None, False, False, prefix + "-show")
        self.panel_event = self.kernel.CreateEventW(None, False, False, prefix + "-tray-panel")
        self.exit_event = self.kernel.CreateEventW(None, False, False, prefix + "-exit")
        ctypes.set_last_error(0)
        self.mutex = self.kernel.CreateMutexW(None, False, prefix + "-window")
        error = ctypes.get_last_error()
        if not self.event or not self.panel_event or not self.exit_event or not self.mutex:
            self.close()
            raise OSError("无法创建桌面窗口标识。")
        self.existing = error == 183  # ERROR_ALREADY_EXISTS
        self.activations = 0
        self.stop = threading.Event()

    def signal(self):
        self.kernel.SetEvent(self.event)

    def signal_panel(self):
        self.kernel.SetEvent(self.panel_event)

    def watch(self, show, exit_window=None, show_panel=None):
        handles = (wintypes.HANDLE * 3)(self.exit_event, self.panel_event, self.event)
        while not self.stop.is_set():
            signaled = self.kernel.WaitForMultipleObjects(3, handles, False, 250)
            if signaled == 0 and exit_window:
                exit_window()
                return
            if signaled == 1 and show_panel:
                show_panel()
            if signaled == 2:
                self.activations += 1
                show()

    def close(self):
        if hasattr(self, "stop"):
            self.stop.set()
        for name in ("event", "panel_event", "exit_event", "mutex"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)


def run(arguments):
    if os.name != "nt":
        raise RuntimeError("桌面窗口当前支持 Windows。其他平台可使用 --background 启动服务。")
    parser = argument_parser()
    parser.add_argument("--desktop-check", type=Path, help="isolated packaged UI verification report")
    parser.add_argument("--desktop-check-ui", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--tray-panel", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tray-preload", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    profile = load_profile()
    ui_dir = profile.ui_dir
    if options.desktop_check:
        report = options.desktop_check.resolve()
        if (options.data_dir.resolve() == default_data_dir().resolve()
                or not report.is_relative_to(options.data_dir.resolve())):
            raise ValueError("桌面验证只允许使用隔离的数据目录。")
    if options.desktop_check_ui:
        if (not options.desktop_check or installed_profile(options.data_dir).get("mode", profile.mode) != "dev"
                or not options.desktop_check_ui.resolve().is_relative_to(options.data_dir.resolve())):
            raise ValueError("测试前端只允许用于隔离的开发版桌面验证。")
        ui_dir = validate_ui_dir(options.desktop_check_ui.resolve())
    instance = SingleWindow(options.data_dir)
    ui = None
    try:
        if instance.existing:
            if options.tray_panel:
                instance.signal_panel()
            elif not options.tray_preload:
                instance.signal()
            return 0
        profile = resolve_profile(profile, options.data_dir)
        if not options.desktop_check_ui:
            ui_dir = profile.ui_dir
        status = ensure_service(options)
        remember_profile(options.data_dir, profile)
        ui = DesktopUI(options.port, ui_dir=ui_dir, live_reload=profile.development,
                       exit_data_dir=options.data_dir).start()
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["DRAG_REGION_SELECTOR"] = ".desktop-titlebar .app-header, .desktop-titlebar .brand, .desktop-titlebar .brand strong"
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        # Explicitly require Chromium. Do not fall back to an external browser or IE.
        title = "Codex 请求优化器 · 开发版" if profile.development else "Codex 请求优化器"
        chrome = WindowChrome()
        main_loaded = not (options.tray_panel or options.tray_preload)
        window = webview.create_window(title, ui.ui_url if main_loaded else "about:blank",
                                       **window_geometry(ui_dir), maximized=False,
                                       js_api=chrome, text_select=True, background_color="#f5f7fb", hidden=bool(options.desktop_check or not main_loaded))
        chrome._bind(window)
        loaded = threading.Event()
        lifecycle = WindowLifecycle(window, lambda: tray_available(options.data_dir, status))
        window.events.loaded += loaded.set

        def show():
            nonlocal main_loaded
            try:
                if not main_loaded:
                    main_loaded = True
                    window.load_url(ui.ui_url)
                ui.window_opened += 1
                lifecycle.show()
            except Exception:
                pass

        from .tray_panel import TrayPanel
        panel = TrayPanel(webview.create_window, ui.ui_url, options.data_dir, show)
        window.events.closed += panel.destroy

        def workers():
            threading.Thread(target=instance.watch, args=(show, lifecycle.exit, panel.show), daemon=True).start()
            if options.tray_panel:
                panel.show()
            else:
                panel.prepare()
            if not lifecycle.wait_loaded(loaded):
                if options.desktop_check and not lifecycle.exiting.is_set() and not lifecycle.closed.is_set():
                    options.desktop_check.write_text(json.dumps({"passed": False, "error": "page_load_timeout"}), encoding="utf-8")
                    lifecycle.exit()
                return
            if options.desktop_check:
                if options.desktop_check.with_suffix(".hold").exists():
                    from .desktop_upgrade_check import check_window
                    check_window(window, options, instance)
                else:
                    from .desktop_check import check_window
                    check_window(window, options, instance, ui)
                return
            # Closing hides the UI; the tray or an upgrade owns its real exit.
            while not lifecycle.closed.wait(1):
                try:
                    local_api(options.port, "/health")
                except (OSError, ValueError):
                    lifecycle.exit()
                    return

        webview.start(workers, gui="edgechromium", private_mode=True, debug=False, icon=str(APP_ICON))
        return 0
    finally:
        if ui is not None:
            ui.close()
        instance.stop.set()
        # Let the wait complete before closing its Windows handle.
        time.sleep(.27)
        instance.close()
