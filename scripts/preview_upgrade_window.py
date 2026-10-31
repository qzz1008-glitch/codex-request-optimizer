"""Persistent developer preview of the real installer, with simulated checkpoints only."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from types import ModuleType

from packet_inspector import VERSION


SOURCE = Path(__file__).resolve().parents[1] / "packet_inspector" / "upgrade_window.py"
STATES = (
    ("校验 · 0%", 0, "正在校验升级包…", None),
    ("准备 · 17%", 1, "升级包校验通过，正在准备新版前后端…", None),
    ("安装 · 33%", 2, "正在启动新版服务并检查运行状态…", None),
    ("切换 · 50%", 3, "新版服务已就绪，正在准备切换…", None),
    ("刷新入口 · 67%", 4, "正在更新托盘、快捷方式和启动入口…", None),
    ("整理 · 83%", 5, "正在整理版本与安装记录…", None),
    ("升级完成 · 100%", 6, "新版已就绪。", True),
    ("升级失败", 4, "新版中转健康检查超时，原服务保持运行。", False),
    ("打开失败", 6, "新版已就绪。", True),
)


def load_view(source):
    # Compile the saved file directly: color edits can keep the same byte length
    # within one second, which can otherwise reuse timestamp-based Python caches.
    module = ModuleType("packet_inspector._upgrade_preview")
    module.__package__, module.__file__ = "packet_inspector", str(SOURCE)
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    return module


class UpgradePreview:
    def __init__(self, window, *, language="zh", state=3, source=SOURCE):
        self.window, self.source = window, Path(source)
        self.saved = self.source.read_bytes()
        self.module = load_view(self.saved)
        self.state, self.language, self.timer, self.playing = state, language, None, False
        self.watch_timer = None
        self.controls = tk.Toplevel(window)
        self.controls.withdraw()
        self.controls.title("升级窗口预览 · 控制")
        self.controls.resizable(False, False)
        self.controls.transient(window)
        panel = ttk.Frame(self.controls, padding=(12, 10))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        self.selector = ttk.Combobox(panel, values=[row[0] for row in STATES], state="readonly", width=21)
        self.selector.current(state)
        self.selector.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.selector.bind("<<ComboboxSelected>>", lambda _event: self.select(self.selector.current()))
        languages = ttk.Frame(panel)
        languages.grid(row=0, column=1)
        self.lang = tk.StringVar(value=language)
        for value, label in (("zh", "中文"), ("en", "English")):
            ttk.Radiobutton(languages, text=label, variable=self.lang, value=value, command=self.change_language).pack(side="left")
        self.play_button = ttk.Button(panel, text="播放演示", command=self.toggle_play)
        self.play_button.grid(row=0, column=2, padx=(10, 0))
        self.note = tk.Label(panel, text="仅模拟流程 · 保存 upgrade_window.py 后自动刷新", anchor="w", height=1, width=1,
                             fg="#748093", font=("Microsoft YaHei UI", 9))
        self.note.grid(row=1, column=0, columnspan=3, pady=(8, 0), sticky="ew")
        self.controls.protocol("WM_DELETE_WINDOW", self.close)
        self.rebuild()
        self.watch_timer = self.window.after(750, self.watch)

    def rebuild(self):
        for child in self.window.winfo_children():
            if child is not self.controls:
                child.destroy()
        self.view = self.module.UpgradeWindow(self.window, 6, self.language,
            f"v{VERSION} · 12345678", f"v{VERSION} · 9abcdef0")
        _, completed, message, finished = STATES[self.state]
        self.view.update_progress(completed, message)
        if finished is not None:
            self.view.finish(finished, message)
        if self.state == 8:
            self.view.set_detail(self.view.t("无法打开优化器：") + self.view.t("新版中转未能启动，原服务保持运行。"), error=True)
        # The production window deliberately blocks close during installation.
        # Preview owns no installer, so its native X / Escape always works.
        self.window.title(self.window.title() + " · 预览")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.controls.bind("<Escape>", lambda _event: self.close())
        self.window.update_idletasks()
        self.window.geometry(f"{self.view.u(536)}x{max(self.view.u(404), self.window.winfo_reqheight())}")

    def show(self):
        self.view.show()
        self.controls.update_idletasks()
        left, top, right, bottom = self.module._work_area(self.window)
        width, height = self.window.winfo_width(), self.window.winfo_height()
        frame = max(24, self.window.winfo_rooty() - self.window.winfo_y())
        controls_height = self.controls.winfo_reqheight()
        combined = height + controls_height + frame * 2 + 10
        x = max(left, min(self.window.winfo_x(), right - width))
        y = max(top, top + (bottom - top - combined) // 2)
        controls_y = min(bottom - controls_height - frame, y + height + frame + 10)
        self.window.geometry(f"{x:+d}{y:+d}")
        self.controls.geometry(f"{width}x{controls_height}{x:+d}{controls_y:+d}")
        self.controls.deiconify()
        if os.name == "nt":
            # Tk's negative geometry is relative to a screen edge. Native moves
            # use the real virtual-desktop coordinates on left-hand monitors.
            for window, pos_y in ((self.window, y), (self.controls, controls_y)):
                window.update_idletasks()
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.GetAncestor.argtypes, user.GetAncestor.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p
                user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user.SetWindowPos(user.GetAncestor(window.winfo_id(), 2), None, x, pos_y, 0, 0, 0x15)
        self.window.lift()
        self.controls.lift()

    def select(self, state):
        self.pause()
        self.state = state
        self.selector.current(state)
        self.rebuild()

    def change_language(self):
        self.language = self.lang.get()
        self.rebuild()

    def pause(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.timer, self.playing = None, False
        self.play_button.configure(text="播放演示")

    def toggle_play(self):
        if self.playing:
            self.pause()
            return
        if self.state >= 6:
            self.select(0)
        self.playing = True
        self.play_button.configure(text="暂停演示")
        self.timer = self.window.after(1300, self.advance)

    def advance(self):
        self.timer = None
        self.state = min(6, self.state + 1)
        self.selector.current(self.state)
        self.rebuild()
        if self.state == 6:
            self.pause()
        elif self.playing:
            self.timer = self.window.after(1300, self.advance)

    def reload_if_changed(self):
        try:
            saved = self.source.read_bytes()
            if saved != self.saved:
                self.saved = saved
                module = load_view(saved)
                previous = self.module
                self.module = module
                try:
                    self.rebuild()
                except Exception:
                    self.module = previous
                    self.rebuild()
                    raise
                self.note.configure(text="已重载 · 仅模拟升级流程", fg="#748093")
        except Exception as exc:
            self.note.configure(text="重载失败：" + str(exc).splitlines()[0], fg="#bc3d37")

    def watch(self):
        self.reload_if_changed()
        self.watch_timer = self.window.after(750, self.watch)

    def close(self):
        self.pause()
        if self.watch_timer is not None:
            self.window.after_cancel(self.watch_timer)
            self.watch_timer = None
        self.window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--state", type=int, choices=range(len(STATES)), default=3)
    options = parser.parse_args(argv)
    if os.name == "nt":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            pass
    window = tk.Tk()
    window.withdraw()
    preview = UpgradePreview(window, language=options.language, state=options.state)
    preview.show()
    window.mainloop()


if __name__ == "__main__":
    main()
