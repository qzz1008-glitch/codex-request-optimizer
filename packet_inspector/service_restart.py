"""Hand tray restarts to the desktop's styled confirmation dialog."""
from __future__ import annotations

from http.client import HTTPException
import json
from pathlib import Path
import re
import secrets
import threading
import time

from .router import control_worker, worker_info


REQUEST_FILE = "desktop-service-restart.json"
REQUEST_TTL = 300
STATE_ERROR = "无法读取中转服务状态，请稍后重新点击“重启服务”。"


def _target(data_dir):
    state = json.loads((data_dir / "router-state.json").read_text(encoding="utf8"))
    identity = state["active"]
    worker = worker_info(data_dir, identity)
    runtime = json.loads((data_dir / "router-workers" / (identity + ".runtime.json")).read_text(encoding="utf8"))
    pid = runtime["pid"]
    if type(pid) is not int or pid <= 0 or runtime.get("port") != worker["port"]:
        raise ValueError("invalid restart target")
    return identity, pid


def restart_from_tray(data_dir, open_window):
    """Queue an intent and show the main window; never restart from a tray click."""
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    intent = {"id": secrets.token_hex(16), "created": time.time()}
    try:
        intent["worker_id"], intent["pid"] = _target(folder)
    except (OSError, ValueError, KeyError, TypeError):
        intent["error"] = STATE_ERROR
    path = folder / REQUEST_FILE
    temporary = path.with_name(path.name + "." + intent["id"] + ".tmp")
    try:
        temporary.write_text(json.dumps(intent), encoding="utf8")
        temporary.replace(path)
        open_window()
    except Exception:
        try:
            if json.loads(path.read_text(encoding="utf8")).get("id") == intent["id"]:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            pass
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "requested": True}


class RestartRequests:
    def __init__(self, data_dir):
        self.folder = Path(data_dir)
        self.path = self.folder / REQUEST_FILE
        self.lock = threading.Lock()
        self.pending = None
        self.busy = False

    def current(self):
        with self.lock:
            claimed = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                self.path.replace(claimed)
                with claimed.open("r", encoding="utf8") as stream:
                    raw = stream.read(1025)
                value = json.loads(raw) if len(raw) <= 1024 else None
                if (isinstance(value, dict) and isinstance(value.get("id"), str)
                        and re.fullmatch(r"[0-9a-f]{32}", value["id"])
                        and type(value.get("created")) in (int, float)
                        and -5 <= time.time() - value["created"] <= REQUEST_TTL
                        and (value.get("error") == STATE_ERROR
                             or isinstance(value.get("worker_id"), str)
                             and re.fullmatch(r"[0-9a-f]{32}", value["worker_id"])
                             and type(value.get("pid")) is int and value["pid"] > 0)):
                    if self.pending is None:
                        self.pending = value
            except (OSError, ValueError, UnicodeError):
                pass
            finally:
                try:
                    claimed.unlink(missing_ok=True)
                except OSError:
                    pass
            if self.pending and not self.busy and time.time() - self.pending["created"] > REQUEST_TTL:
                self.pending = None
            return {"id": self.pending["id"], "error": self.pending.get("error", "")} if self.pending else None

    def begin(self, request_id):
        with self.lock:
            if not self.pending or request_id != self.pending["id"] or time.time() - self.pending["created"] > REQUEST_TTL:
                raise ValueError("重启请求已失效，请重新从托盘点击“重启服务”。")
            if self.busy:
                raise ValueError("正在处理重启请求，请稍候。")
            self.busy = True
            return dict(self.pending)

    def finish(self, *, completed):
        with self.lock:
            self.busy = False
            if completed:
                self.pending = None


def perform_restart(data_dir, intent):
    """Only a confirmed desktop intent may restart the same worker instance."""
    if intent.get("error"):
        raise ValueError(STATE_ERROR)
    folder = Path(data_dir)
    identity, pid = intent["worker_id"], intent["pid"]
    try:
        current = _target(folder)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(STATE_ERROR) from exc
    if current != (identity, pid):
        raise ValueError("服务状态已变化，请重新从托盘点击“重启服务”。")
    try:
        result = control_worker(folder, identity, "restart", expected_pid=pid)
        if not isinstance(result, dict) or result.get("restarting") is not True:
            raise ValueError("未能确认服务重启，请稍后重试。")
    except (OSError, ValueError, KeyError, TypeError, HTTPException) as exc:
        raise ValueError("未能确认服务重启，请稍后重试。") from exc
    return {"ok": True, "restarting": True}
