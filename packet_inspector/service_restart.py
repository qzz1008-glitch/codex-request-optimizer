"""Confirm a tray restart, then let the existing entry recover its active worker."""
from __future__ import annotations

import ctypes
from http.client import HTTPException
import json
from pathlib import Path

from .router import control_worker, worker_info


def _dialog(message, title, *, confirm=False):
    # Default to No; closing the dialog never authorizes a restart.
    flags = 0x10000 | (0x04 | 0x30 | 0x100 if confirm else 0x10)
    return ctypes.windll.user32.MessageBoxW(0, message, title, flags) == 6


def _target(data_dir):
    state = json.loads((data_dir / "router-state.json").read_text(encoding="utf8"))
    identity = state["active"]
    worker = worker_info(data_dir, identity)
    runtime = json.loads((data_dir / "router-workers" / (identity + ".runtime.json")).read_text(encoding="utf8"))
    pid = runtime["pid"]
    if type(pid) is not int or pid <= 0 or runtime.get("port") != worker["port"]:
        raise ValueError("invalid restart target")
    return identity, pid


def restart_from_tray(data_dir):
    """Fixed local action shared by both tray menus; never changes configuration."""
    folder = Path(data_dir)
    try:
        english = json.loads((folder / "settings.json").read_text(encoding="utf8")).get("ui_language") == "en"
    except (OSError, ValueError, AttributeError):
        english = False
    title = "Restart service" if english else "重启服务"
    message = ("Restart the relay service?\n\nRequest forwarding will briefly stop, and requests in progress may fail. "
               "You can continue chatting once the service recovers."
               if english else "是否重启中转服务？\n\n重启期间将短暂中断请求转发，正在进行的请求可能失败。服务恢复后可继续对话。")
    try:
        identity, pid = _target(folder)
        if not _dialog(message, title, confirm=True):
            return {"ok": True, "cancelled": True}
        # An upgrade or another restart during confirmation invalidates this action.
        if _target(folder) != (identity, pid):
            raise ValueError("restart target changed")
        result = control_worker(folder, identity, "restart", expected_pid=pid)
        if not isinstance(result, dict) or result.get("restarting") is not True:
            raise ValueError("restart not acknowledged")
        return {"ok": True, "restarting": True}
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        message = ("Could not confirm the service restart. Please try again shortly. "
                   "If the service remains unavailable, quit and reopen this application."
                   if english else "未能确认服务重启，请稍后重试。若仍无法使用，请退出并重新打开本软件。")
        _dialog(message, title)
        return {"ok": False, "error": message}
