"""Hand a tray exit request to the desktop window before stopping the relay."""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import threading
import time


REQUEST_FILE = "desktop-exit-request.json"
REQUEST_TTL = 300


def request_exit(data_dir, open_window):
    """Only the tray creates an exit intent; opening the UI never stops a service."""
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    intent = {"id": secrets.token_hex(16), "created": time.time()}
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


class ExitRequests:
    """Keep one confirmed tray intent per desktop; ordinary previews have none."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / REQUEST_FILE
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
                        and -5 <= time.time() - value["created"] <= REQUEST_TTL):
                    if self.pending is None:
                        self.pending = value["id"]
            except (OSError, ValueError, UnicodeError):
                pass
            finally:
                try:
                    claimed.unlink(missing_ok=True)
                except OSError:
                    pass
            return self.pending

    def begin(self, request_id):
        with self.lock:
            if not self.pending or request_id != self.pending:
                raise ValueError("退出请求已失效，请重新从托盘退出。")
            if self.busy:
                raise ValueError("正在处理退出请求，请稍候。")
            self.busy = True

    def finish(self, *, completed):
        with self.lock:
            self.busy = False
            if completed:
                self.pending = None


class ExitError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def perform_exit(control, action):
    """Restore before stopping, and leave the relay running if any step fails."""
    if action not in {"restore", "direct", "disconnected"}:
        raise ExitError("无效的退出操作。", 400)

    def call(path, data=None):
        status, result = control(path, data)
        if status != 200 or not isinstance(result, dict):
            reason = result.get("error") if isinstance(result, dict) else None
            if reason == "requests_in_flight":
                raise ExitError("仍有请求正在转发，中转服务继续运行。请在请求完成后重试。")
            raise ExitError("操作未完成，中转服务继续运行。请稍后重试。", 502)
        return result

    status = call("/api/status")
    if any(isinstance(status.get(key), (int, float)) and status[key] > 0
           for key in ("active_requests", "relay_active_requests", "relay_requests")):
        raise ExitError("仍有请求正在转发，中转服务继续运行。请在请求完成后重试。")
    if action == "disconnected":
        profile = status.get("profile", {})
        if (not isinstance(profile, dict) or profile.get("active") is not False
                or profile.get("config_error") or status.get("provider_sync_error")):
            raise ExitError("接入状态已变化，请确认后再退出。")
    if action == "restore":
        profile = status.get("profile", {})
        if (not isinstance(profile, dict) or type(profile.get("active")) is not bool
                or profile.get("config_error") or status.get("provider_sync_error")):
            raise ExitError("无法确认接入状态，中转服务继续运行。请检查配置后重试。")
        if profile["active"]:
            if not profile.get("can_restore"):
                raise ExitError("无法恢复原出口，中转服务继续运行。请检查配置后重试。")
            call("/api/link", {"action": "restore"})
            restored = call("/api/status")
            profile = restored.get("profile", {})
            if (not isinstance(profile, dict) or profile.get("active") is not False
                    or profile.get("config_error") or restored.get("provider_sync_error")):
                raise ExitError("尚未切换为不接入，中转服务继续运行。请重试。")
    stopped = call("/api/stop", {})
    if stopped.get("stopped") is not True:
        raise ExitError("退出未完成，请稍后重试。", 502)
    return {"stopped": True, "restored": action == "restore"}
