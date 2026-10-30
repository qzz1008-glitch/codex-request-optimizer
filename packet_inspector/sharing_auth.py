"""Private, local-only access to Codex-managed authorization. No login store."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


class SharingError(RuntimeError):
    pass


def native_command():
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if not executable and os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        candidates = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        executable = str(candidates[0]) if candidates else None
    if not executable:
        raise SharingError("未找到 Codex 本机程序，请先安装或更新 Codex。")
    return [executable, "app-server", "-c", 'model_provider="openai"']


class NativeAuthorization:
    """The native process owns persistence/renewal; only short-lived tokens enter memory."""

    def __init__(self, command=None, timeout=20, *, native_home=None):
        self.command = command
        self.timeout = timeout
        self.native_home = Path(native_home).resolve() if native_home is not None else None
        self.guard = threading.RLock()
        self.process = None
        self.messages = None
        self.sequence = 0

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        if self.process is not None:
            self.close()
        self.messages = queue.Queue(maxsize=32)
        environment = {**os.environ, "CODEX_HOME": str(self.native_home)} if self.native_home else None
        self.process = subprocess.Popen(self.command or native_command(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=environment)
        process, messages = self.process, self.messages

        def receive():
            try:
                for line in process.stdout:
                    if len(line) > 1_000_000:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and "id" in item:
                        try:
                            messages.put_nowait(item)
                        except queue.Full:
                            break
            finally:
                try:
                    messages.put_nowait(None)
                except queue.Full:
                    pass

        threading.Thread(target=receive, daemon=True).start()
        self._call("initialize", {"clientInfo": {"name": "codex_inspector_sharing", "version": "1"}})
        self.process.stdin.write('{"method":"initialized"}\n')
        self.process.stdin.flush()

    def _call(self, method, params):
        self.sequence += 1
        number = self.sequence
        self.process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            item = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            if item is None:
                raise SharingError("Codex 本机授权接口已关闭，请重新开启共享。")
            if item.get("id") != number:
                if time.monotonic() >= deadline:
                    raise queue.Empty
                continue
            if "error" in item:
                raise SharingError("当前 Codex 未提供所需的本机授权接口，请更新 Codex。")
            return item.get("result", {})

    @staticmethod
    def _credentials(result):
        if not isinstance(result, dict):
            raise SharingError("Codex 返回的授权无效。")
        token = result.get("authToken")
        mode = result.get("authMethod")
        if not isinstance(token, str) or not token or mode not in {"chatgpt", "apikey"}:
            raise SharingError("Codex 当前没有可共享的本机授权，请先在 Codex 登录。")
        if "\r" in token or "\n" in token:
            raise SharingError("Codex 返回的授权无效。")
        account, expiry = "", None
        if mode == "chatgpt":
            try:
                part = token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
                expiry = float(claims["exp"])
                if not isinstance(account, str) or not account or "\r" in account or "\n" in account:
                    raise ValueError()
            except (ValueError, KeyError, IndexError, TypeError):
                raise SharingError("Codex 返回的授权不包含有效的账号标识。") from None
        headers = {"Authorization": "Bearer " + token}
        if account:
            headers["ChatGPT-Account-Id"] = account
        return {"mode": mode, "identity": account or token, "expiry": expiry, "headers": headers}

    def credentials(self):
        with self.guard:
            try:
                self._start()
                value = self._credentials(self._call("getAuthStatus", {"includeToken": True, "refreshToken": False}))
                if value["expiry"] is not None and value["expiry"] < time.time() + 120:
                    value = self._credentials(self._call("getAuthStatus", {"includeToken": True, "refreshToken": True}))
                    if value["expiry"] < time.time() + 10:
                        raise SharingError("Codex 授权已过期，请在 Codex 重新登录。")
                return value
            except SharingError:
                self.close()
                raise
            except (OSError, ValueError, queue.Empty, BrokenPipeError):
                self.close()
                raise SharingError("无法读取 Codex 本机授权，请确认 Codex 可正常登录后重试。") from None

    def close(self):
        with self.guard:
            process, self.process = self.process, None
            if process is not None:
                try:
                    process.stdin.close()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=2)
                finally:
                    process.stdout.close()
