"""A small, DPI-aware installer window driven by actual upgrade checkpoints."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk

from .app_icon import APP_ICON


COLORS = {"paper": "#ffffff", "ink": "#1e293b", "muted": "#748093",
          "line": "#e3e7ed", "accent": "#315fd7", "soft": "#edf2fc",
          "success": "#247558", "error": "#bc3d37"}
ENGLISH = {
    "安装与升级": "Install & update",
    "Codex请求优化器 更新中": "Updating Codex Request Optimizer", "升级已完成": "Update complete", "升级未完成": "Update interrupted",
    "校验与准备": "Prepare", "安装新版": "Install", "切换服务": "Switch", "完成": "Finish",
    "新版已就绪。": "The new version is ready.",
    "正在打开新版应用…": "Opening the updated application…",
    "无法打开优化器：": "Could not open the optimizer: ",
    "正在校验升级包…": "Verifying the update package…",
    "升级包校验通过，正在准备新版前后端…": "Package verified. Preparing the updated application…",
    "正在启动新版服务并检查运行状态…": "Starting the new service and checking its health…",
    "新版服务已就绪，正在准备切换…": "The new service is ready. Preparing the handover…",
    "正在更新桌面窗口，固定入口继续转发…": "Updating the desktop window. The relay entry remains available…",
    "新版已就绪，正在切换新请求；旧请求继续完成…": "Switching new requests. Existing requests continue to completion…",
    "正在交接额度与网络共享，已有连接继续完成…": "Handing over quota and network sharing. Existing connections continue…",
    "当前运行版本不支持共享无缝交接，已保留当前服务。请先停止共享或断开借入，再升级一次。": "This running version cannot hand over sharing without interruption. The service was preserved. Stop sharing and disconnect borrowed connections before this one-time update.",
    "正在更新托盘、快捷方式和启动入口…": "Refreshing the tray, shortcuts and startup entry…",
    "正在关闭旧版优化器窗口，中转继续运行…": "Closing the previous window. The relay remains running…",
    "首次安装固定入口，正在等待旧服务空闲…": "Installing the stable entry. Waiting for existing requests to finish…",
    "启动固定入口并检查前后端…": "Starting the stable entry and checking the application…",
    "升级未完成，正在恢复旧入口…": "The update could not finish. Restoring the previous entry…",
    "正在整理版本与安装记录…": "Finishing version cleanup and installation records…",
    "升级完成。旧服务会在请求结束后退出。": "Update complete. The old service exits after its requests finish.",
    "升级包的入口协议不兼容。": "The update package uses an incompatible entry protocol.",
    "升级包的入口模式不兼容。": "The update package uses an incompatible entry mode.",
    "升级包校验失败。": "The update package failed verification.",
    "已暂存的新版程序校验失败。": "The staged application failed verification.",
    "入口端口无效。": "The entry port is invalid.",
    "现有服务暂不可用，未替换入口。": "The existing service is unavailable. Its entry was not replaced.",
    "端口属于其他服务或配置，未修改它。": "This port belongs to another service or configuration. It was left unchanged.",
    "旧版不支持安全排空，请先手动关闭旧中转再运行升级程序。": "This older version cannot drain requests safely. Close its relay before updating.",
    "入口协议不兼容，未修改正在运行的服务。": "The entry protocol is incompatible. The running service was left unchanged.",
    "新版中转未能启动，原服务保持运行。": "The new relay could not start. The previous service is still running.",
    "新版中转健康检查超时，原服务保持运行。": "The new relay health check timed out. The previous service is still running.",
    "切换后验证失败。": "Post-handover verification failed.", "接入配置验证失败。": "Connection configuration verification failed.",
    "托盘交接状态待确认，已保留当前中转。": "Tray handover could not be confirmed. The active relay was preserved.",
    "旧服务仍有请求，已取消本次升级。现有中转继续运行。": "The previous service still has active requests. The update was cancelled and the relay remains running.",
    "旧服务尚未退出，取消替换。": "The previous service has not exited. Replacement was cancelled.",
    "旧监听端口未释放，取消替换。": "The previous listener has not released its port. Replacement was cancelled.",
    "入口服务健康检查超时。": "The entry service health check timed out.",
    "旧后台恢复超时，已保留当前中转。": "Restoring the previous service timed out. The active relay was preserved.",
    "无法确认旧程序状态，升级未中断现有中转。": "The previous application state could not be confirmed. The existing relay was not interrupted.",
    "新版入口仍在运行，未覆盖它的程序或配置，请查看升级结果。": "The new entry is still running. Its application and configuration were not overwritten. Check the upgrade result.",
}


def window_metadata(engine):
    def read(name):
        try:
            value = json.loads(Path(name).read_text(encoding="utf8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def version(value, build=None):
        if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"\d+\.\d+\.\d+[\w.+-]{0,32}", value):
            return ""
        suffix = " · " + build[:8] if isinstance(build, str) and re.fullmatch(r"[0-9a-f]{64}", build) else ""
        return "v" + value + suffix

    language = "en" if read(engine.data_dir / "settings.json").get("ui_language") == "en" else "zh"
    runtime, manifest = read(engine.data_dir / "runtime.json"), read(engine.payload / "manifest.json")
    current_build = None
    identity = runtime.get("worker_id")
    if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{32}", identity):
        worker = read(engine.data_dir / "router-workers" / (identity + ".json"))
        executable = worker.get("executable")
        if worker.get("id") == identity and isinstance(executable, str) and executable:
            try:
                # Managed version directories are named after their verified EXE hash.
                path = Path(executable).resolve()
                if path.is_relative_to((engine.data_dir / "bin" / "versions").resolve()):
                    current_build = path.parent.name
            except (OSError, ValueError):
                pass
    hashes = manifest.get("sha256")
    target_build = hashes.get("CodexPacketInspector.exe") if isinstance(hashes, dict) else None
    return language, version(runtime.get("version"), current_build), version(manifest.get("version"), target_build)


def _work_area(window):
    fallback = (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())
    if os.name != "nt":
        return fallback
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.MonitorFromPoint.argtypes, user.MonitorFromPoint.restype = [wintypes.POINT, wintypes.DWORD], wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        point, info = wintypes.POINT(), MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetCursorPos(ctypes.byref(point)) and user.GetMonitorInfoW(user.MonitorFromPoint(point, 2), ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
    except (OSError, AttributeError):
        pass
    return fallback


class UpgradeWindow:
    """All widget changes stay on Tk's main thread; only completed work fills the bar."""

    def __init__(self, window, total_steps, language="zh", current="", target=""):
        self.window, self.total_steps, self.language = window, total_steps, language
        self.completed, self.state = 0, "running"
        self.scale = window.winfo_fpixels("1i") / 96
        self.font = "Segoe UI" if language == "en" else "Microsoft YaHei UI"
        window.title(self.t("安装与升级"))
        window.configure(background=COLORS["paper"])
        window.resizable(False, False)
        try:
            window.iconbitmap(str(APP_ICON))
        except tk.TclError:
            pass
        style = ttk.Style(window)
        style.theme_use("clam")
        style.layout("Upgrade.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"sticky": "nswe", "expand": "1"})]})])
        style.configure("Upgrade.Vertical.TScrollbar", width=self.u(6), borderwidth=0, arrowsize=0,
                        background=COLORS["line"], troughcolor=COLORS["paper"], bordercolor=COLORS["paper"])
        body = tk.Frame(window, bg=COLORS["paper"])
        body.pack(fill="both", expand=True, padx=self.u(28), pady=(self.u(20), self.u(20)))
        self.heading = self.label(body, "Codex请求优化器 更新中", size=18, weight="bold")
        self.heading.pack(anchor="w", pady=(0, self.u(6)))
        versions = tk.Frame(body, bg=COLORS["paper"])
        versions.pack(anchor="w", fill="x")
        if current:
            self.label(versions, current, size=10, color="muted").pack(side="left")
            self.label(versions, "  →  ", size=11, color="muted").pack(side="left")
        self.version_label = self.label(versions, target or "—", size=10, color="accent", weight="bold")
        self.version_label.configure(bg=COLORS["soft"], padx=self.u(8), pady=self.u(3))
        self.version_label.pack(side="left")
        progress = tk.Frame(body, bg=COLORS["paper"])
        progress.pack(fill="x", pady=(self.u(12), self.u(8)))
        self.timeline = tk.Canvas(progress, bg=COLORS["paper"], height=self.u(50), highlightthickness=0)
        self.timeline.pack(fill="x", pady=(self.u(2), 0))
        details = tk.Frame(body, bg=COLORS["paper"])
        details.pack(fill="both", expand=True)
        details.columnconfigure(0, weight=1)
        details.columnconfigure(1, minsize=self.u(10))
        details.rowconfigure(0, weight=1)
        self.detail = tk.Text(details, width=1, height=2, font=(self.font, 10), wrap="word", bg=COLORS["paper"], fg=COLORS["muted"],
                              relief="flat", borderwidth=0, highlightthickness=0, padx=0, pady=0, cursor="arrow", state="disabled")
        self.detail.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(details, orient="vertical", style="Upgrade.Vertical.TScrollbar", command=self.detail.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(self.u(4), 0))
        self.detail.configure(yscrollcommand=self.scroll_detail)
        self.timeline.bind("<Configure>", lambda _event: self.draw_progress())
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _event: self.close())
        self.update_progress(0, "正在校验升级包…")

    def u(self, value):
        return round(value * self.scale)

    def t(self, text):
        return ENGLISH.get(text, text) if self.language == "en" else text

    def label(self, parent, text, size=10, weight="normal", color="ink"):
        return tk.Label(parent, text=self.t(text), font=(self.font, size, weight), bg=COLORS["paper"], fg=COLORS[color], padx=0, pady=0)

    def dimensions(self):
        self.window.update_idletasks()
        return self.u(536), max(self.u(240), self.window.winfo_reqheight())

    def show(self):
        width, height = self.dimensions()
        left, top, right, bottom = _work_area(self.window)
        x, y = left + max(0, (right - left - width) // 2), top + max(0, (bottom - top - height) // 2)
        self.window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.window.attributes("-alpha", 0)
        self.window.deiconify()
        self.window.update_idletasks()
        if os.name == "nt":
            try:
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.GetAncestor.argtypes, user.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
                handle = user.GetAncestor(self.window.winfo_id(), 2)
                user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
                user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
                bounds = wintypes.RECT()
                if handle and user.GetWindowRect(handle, ctypes.byref(bounds)):
                    # Native coordinates also handle monitors left of the primary display.
                    x = left + max(0, (right - left - (bounds.right - bounds.left)) // 2)
                    y = top + max(0, (bottom - top - (bounds.bottom - bounds.top)) // 2)
                    user.SetWindowPos(handle, None, x, y, 0, 0, 0x01 | 0x04 | 0x10)
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
                for attribute, setting in ((20, 0), (33, 2), (35, 0xffffff)):
                    value = ctypes.c_int(setting)
                    dwm.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
            except (OSError, AttributeError):
                pass
        self.window.attributes("-alpha", 1)

    def scroll_detail(self, start, end):
        self.scrollbar.set(start, end)
        if float(start) <= 0 and float(end) >= 1:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()

    def set_detail(self, text, error=False):
        self.detail.configure(state="normal", fg=COLORS["error" if error else "muted"])
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", self.t(text))
        self.detail.configure(state="disabled")

    def update_progress(self, completed, text):
        self.completed = max(self.completed, min(completed, self.total_steps))
        self.set_detail(text)
        self.draw_progress()

    def draw_progress(self):
        color = COLORS[{"complete": "success", "failed": "error"}.get(self.state, "accent")]
        self.timeline.delete("all")
        width, y, radius = self.timeline.winfo_width(), self.u(14), self.u(9)
        if width <= self.u(76):
            return
        fraction = self.completed / self.total_steps
        # Equally spaced milestones use real checkpoint boundaries.
        # Each connecting segment fills as work within that stage completes.
        stops = (0, 1 / 3, 1 / 2, 1)
        positions = [self.u(38) + (width - self.u(76)) * index / 3 for index in range(4)]
        self.timeline.create_line(positions[0], y, positions[-1], y,
                                  fill=COLORS["line"], width=self.u(4), capstyle="round", tags="track")
        phase = max(index for index, stop in enumerate(stops) if fraction >= stop)
        reached = positions[phase]
        if phase < len(stops) - 1:
            within_stage = (fraction - stops[phase]) / (stops[phase + 1] - stops[phase])
            reached += (positions[phase + 1] - positions[phase]) * within_stage
        if self.completed:
            self.timeline.create_line(positions[0], y, reached, y,
                                      fill=color, width=self.u(4), capstyle="round", tags="progress")
        for index, x in enumerate(positions):
            done = self.state == "complete" or index < phase
            active = index == phase
            fill = COLORS["success"] if self.state == "complete" else COLORS["accent"] if done else color if active else COLORS["paper"]
            self.timeline.create_oval(x - radius, y - radius, x + radius, y + radius, fill=fill,
                                      outline=fill if done or active else COLORS["line"], tags="milestone")
            if done:
                self.timeline.create_line(x - self.u(4), y, x - self.u(1), y + self.u(3), x + self.u(5), y - self.u(4),
                                          fill="white", width=self.u(1.5), capstyle="round", joinstyle="round")
            else:
                self.timeline.create_text(x, y, text="×" if active and self.state == "failed" else str(index + 1),
                                          fill="white" if active else COLORS["muted"], font=(self.font, 9))
            self.timeline.create_text(x, self.u(40), text=self.t(("校验与准备", "安装新版", "切换服务", "完成")[index]),
                                      fill=COLORS["ink"] if active or done else COLORS["muted"], font=(self.font, 9), tags="stage-label")

    def finish(self, success, detail=""):
        self.state = "complete" if success else "failed"
        self.heading.configure(text=self.t("升级已完成" if success else "升级未完成"))
        if success:
            self.update_progress(self.total_steps, detail or "新版已就绪。")
        else:
            self.set_detail(detail, error=True)
            self.draw_progress()

    def close(self):
        if self.state != "running":
            self.window.destroy()


def run_upgrade_window(engine, *, open_after=False):
    if os.name == "nt":
        try:
            # This is the separate installer process, before it creates any HWND.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            pass
    window = tk.Tk()
    window.withdraw()
    view = UpgradeWindow(window, engine.total_steps, *window_metadata(engine))
    events = queue.Queue()
    engine.progress = lambda message: events.put(("progress", engine.completed_steps, message))
    def work():
        try:
            engine.run()
        except Exception as exc:
            events.put(("failed", 0, str(exc)))
            return
        if open_after:
            try:
                engine._open_window()
            except Exception as exc:
                events.put(("open_failed", 0, str(exc)))
                return
        events.put(("complete", 0, "正在打开新版应用…" if open_after else ""))
    def refresh():
        while True:
            try:
                state, completed, message = events.get_nowait()
            except queue.Empty:
                break
            if state == "progress":
                view.update_progress(completed, message)
            else:
                view.finish(state != "failed", "" if state == "open_failed" else message)
                if state == "open_failed":
                    view.set_detail(view.t("无法打开优化器：") + view.t(message), error=True)
                if state == "complete" and open_after:
                    window.after(800, window.destroy)
                return
        window.after(100, refresh)
    view.show()
    threading.Thread(target=work, name="application-upgrade", daemon=True).start()
    refresh()
    window.mainloop()
    return 0 if view.state == "complete" else 1
