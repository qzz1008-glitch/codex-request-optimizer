"""Bounded startup snapshots; no request logging, probes or background writer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from . import VERSION
from .build_info import build_info

STARTUP_SECONDS = 120
DEADLINE_ENV = "CODEX_INSPECTOR_STARTUP_UNTIL"
MAX_FILES = 16
MAX_BYTES = 16 * 1024


def startup_environment():
    now = time.time()
    try:
        deadline = float(os.environ.get(DEADLINE_ENV, "0"))
    except ValueError:
        deadline = 0
    if not now < deadline <= now + STARTUP_SECONDS:
        deadline = now + STARTUP_SECONDS
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1", DEADLINE_ENV: str(deadline)}


def startup_wait_seconds():
    try:
        return min(STARTUP_SECONDS, max(0, float(os.environ[DEADLINE_ENV]) - time.time()))
    except (KeyError, ValueError):
        return STARTUP_SECONDS


def log_directory(data_dir):
    root = Path(data_dir)
    try:
        settings = root / "settings.json"
        if settings.stat().st_size <= 1024 * 1024:
            value = json.loads(settings.read_text(encoding="utf8")).get("log_dir", "")
            if isinstance(value, str) and value.strip():
                path = Path(value).expanduser()
                return path if path.is_absolute() else root / path
    except (OSError, ValueError, AttributeError):
        pass
    return root


class StartupTrace:
    def __init__(self, data_dir, role, *, worker_id="", wait_seconds=None):
        self.directory = log_directory(data_dir) / "startup"
        self.path = self.directory / (str(os.getpid()) + "-" + uuid.uuid4().hex[:12] + ".json")
        self.start = self.phase_start = time.monotonic()
        self.record = {"version": 1, "application_version": VERSION, "role": role,
                       "build": build_info(),
                       "pid": os.getpid(), "parent_pid": os.getppid(), "worker_id": worker_id,
                       "executable": Path(os.sys.executable).name, "started": time.time(),
                       "state": "starting", "phase": "initializing", "stages": [],
                       "wait_budget_seconds": round(startup_wait_seconds() if wait_seconds is None else wait_seconds, 3)}
        self._save()

    def step(self, phase, **details):
        if phase == self.record["phase"] and details == self.record.get("details", {}):
            return
        now = time.monotonic()
        self.record["stages"] = [*self.record["stages"],
            {"phase": self.record["phase"], "elapsed_ms": round((now - self.phase_start) * 1000, 1)}][-24:]
        self.record.update(phase=phase, details=details)
        self.phase_start = now
        self._save()

    def ready(self):
        self.record["state"] = "ready"
        self._save()

    def fail(self, exc, **details):
        self.record.update(state="failed", failure_details=details)
        worker = self.record.get("details", {}).get("worker_id")
        if worker:
            self.record["target_startup"] = {"state": "not_reported"}
            try:
                child_pid = self.record["details"].get("child_pid")
                targets = [row for row in startup_records(self.directory.parent)
                           if row.get("role") == "worker" and row.get("worker_id") == worker
                           and (not child_pid or row.get("pid") == child_pid)
                           and isinstance(row.get("started"), (int, float)) and row["started"] >= self.record["started"]]
                target = targets[0] if targets else None
                if target:
                    self.record["target_startup"] = {key: target.get(key) for key in
                        ("pid", "state", "phase", "elapsed_ms", "phase_elapsed_ms", "updated")}
                    if (target.get("state") == "starting" and isinstance(target.get("updated"), (int, float))
                            and isinstance(target.get("phase_elapsed_ms"), (int, float))):
                        self.record["target_startup"]["phase_elapsed_ms"] = round(
                            target["phase_elapsed_ms"] + max(0, time.time() - target["updated"]) * 1000, 1)
            except (OSError, TypeError, ValueError):
                pass
        try:
            # Lazy: startup itself remains independent of request analysis.
            from .network_diagnostics import exception_detail
            self.record["exception"] = exception_detail(exc)
        except Exception:
            self.record["exception"] = [{"type": type(exc).__name__}]
        self._save()

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        try:
            now = time.monotonic()
            self.record.update(updated=time.time(), elapsed_ms=round((now - self.start) * 1000, 1),
                               phase_elapsed_ms=round((now - self.phase_start) * 1000, 1))
            raw = json.dumps(self.record, ensure_ascii=False).encode("utf8")
            if len(raw) > MAX_BYTES:
                self.record["exception"] = [{"type": e.get("type"), "message": e.get("message", "")[:400]}
                                            for e in self.record.get("exception", [])[:4]]
                self.record["stages"] = self.record["stages"][-8:]
                raw = json.dumps(self.record, ensure_ascii=False).encode("utf8")
            if len(raw) > MAX_BYTES:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(raw)
            temporary.replace(self.path)
            files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[MAX_FILES:]:
                path.unlink(missing_ok=True)
            temporaries = sorted(self.directory.glob("*.tmp"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in temporaries[MAX_FILES:]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        except (OSError, ValueError, TypeError):
            pass  # Diagnostic failures must not prevent the relay from starting.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def startup_records(directory):
    result = []
    folder = Path(directory) / "startup"
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_FILES]
        for path in files:
            try:
                if path.stat().st_size <= MAX_BYTES:
                    value = json.loads(path.read_text(encoding="utf8"))
                    if isinstance(value, dict):
                        result.append(value)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return result
