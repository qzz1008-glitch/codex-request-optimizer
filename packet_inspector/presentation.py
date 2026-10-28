"""Keep upgradeable desktop integration in the active application worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading

from .router import atomic_json, worker_info


class WorkerPresentation:
    def __init__(self, server, *, enabled=None, tray_factory=None, retire_entry=None):
        self.server = server
        self.enabled = os.name == "nt" if enabled is None else enabled
        self.tray_factory = tray_factory
        self.retire_entry = retire_entry
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.tray = None
        self.error = ""

    def start(self):
        self.thread = threading.Thread(target=self._monitor, name="worker-presentation", daemon=True)
        self.thread.start()
        return self

    def _state(self):
        value = json.loads((self.server.data_dir / "router-state.json").read_text(encoding="utf8"))
        if Path(value["config"]).resolve() != self.server.link.config.resolve():
            raise ValueError("presentation profile mismatch")
        return value

    def _open_window(self):
        state = self._state()
        worker = worker_info(self.server.data_dir, state["active"])
        executable = Path(worker["executable"]).resolve()
        if not executable.is_relative_to((self.server.data_dir / "bin" / "versions").resolve()) or not executable.is_file():
            raise ValueError("desktop executable is outside managed versions")
        subprocess.Popen([str(executable), "--port", str(self.server.public_port), "--data-dir", str(self.server.data_dir),
                          "--config", str(self.server.link.config)],
                         env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _exit(self):
        from .exit_flow import request_exit
        try:
            request_exit(self.server.data_dir, self._open_window)
        except (OSError, ValueError):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "无法打开退出确认，中转服务继续运行。请稍后重试。", "Codex 请求优化器", 0x40)

    def _ensure_tray(self):
        if not self.enabled:
            return
        if self.tray and self.tray.thread.is_alive():
            return
        if self.tray:
            self._stop_tray()
        self.server.desktop_tray = False
        factory = self.tray_factory
        if factory is None:
            from .tray import NativeTray
            factory = NativeTray
        self.tray = factory(self._open_window, self._exit, status_reader=self.server.tray_status).start()
        self.server.desktop_tray = True

    def _stop_tray(self):
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.server.desktop_tray = False

    def _retire_entry_tray(self):
        if not self.enabled or not self.tray:
            return
        runtime = json.loads((self.server.data_dir / "runtime.json").read_text(encoding="utf8"))
        retire = self.retire_entry
        if retire is None:
            from .tray import close_entry_tray
            retire = close_entry_tray
        retire(runtime.get("pid"), self.server.data_dir / "bin" / "CodexPacketInspector.exe")

    def commit(self):
        """Publish tray ownership only after the backend switch has been verified."""
        with self.guard:
            if self.server.worker_state != "active" or self._state()["active"] != self.server.worker_id:
                raise ValueError("desktop integration requires the active worker")
            self._ensure_tray()
            try:
                worker = worker_info(self.server.data_dir, self.server.worker_id)
                atomic_json(self.server.data_dir / "router-workers" / (self.server.worker_id + ".json"),
                            {**worker, "presentation_committed": True})
            except Exception:
                self._stop_tray()
                raise
            self.error = ""
        # A missing legacy tray is harmless. The monitor retries a late startup.
        self.reconcile()
        return {"desktop_tray": self.server.desktop_tray if self.enabled else False}

    def reconcile(self):
        try:
            with self.guard:
                active = self._state()["active"]
                current = worker_info(self.server.data_dir, active)
                if not current.get("presentation_committed"):
                    return  # Keep the old tray throughout validation and rollback.
                if active == self.server.worker_id and self.server.worker_state == "active":
                    self._ensure_tray()
                    self._retire_entry_tray()
                elif active != self.server.worker_id:
                    self._stop_tray()
                self.error = ""
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            self.error = type(exc).__name__

    def _monitor(self):
        while not self.stop_event.is_set():
            self.reconcile()
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        with self.guard:
            self._stop_tray()
