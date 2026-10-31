"""Small, persistent reminders for native configuration changes, never inference data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import threading

from .locking import file_lock


KEYS = {"connection", "context_tokens", "compact_tokens", "capacity_retry",
        "image_analysis_mode", "entry_port", "sharing", "wsl"}
VALUE_KEYS = {"context_tokens", "compact_tokens", "capacity_retry", "image_analysis_mode", "entry_port"}


class RestartNotices:
    def __init__(self, data_dir, config_path):
        identity = hashlib.sha256(str(Path(config_path).resolve()).casefold().encode()).hexdigest()[:16]
        self.path = Path(data_dir) / ("restart-notice-" + identity + ".json")
        self.guard = threading.RLock()
        self.error = False
        self.pending = []
        self.memory = {"revision": 0, "items": {}}

    def _read(self):
        if not self.path.exists():
            return {"revision": 0, "items": {}}
        if self.path.stat().st_size > 65536:
            raise ValueError("invalid restart notice")
        data = json.loads(self.path.read_text(encoding="utf8"))
        if (not isinstance(data, dict) or type(data.get("revision")) is not int
                or data["revision"] < 0 or not isinstance(data.get("items"), dict)
                or len(data["items"]) > 256):
            raise ValueError("invalid restart notice")
        for identity, item in data["items"].items():
            if (not isinstance(item, dict) or item.get("key") not in KEYS
                    or item.get("target") not in {"chatgpt", "wsl"}
                    or not isinstance(item.get("scope"), str) or len(item["scope"]) > 160
                    or set(item) - {"key", "target", "scope", "before", "after"}
                    or identity != item["target"] + ":" + item["key"] + ":" + item["scope"]):
                raise ValueError("invalid restart notice")
        return data

    def snapshot(self):
        with self.guard:
            if not self.pending:
                try:
                    self.memory = self._read()
                    self.error = False
                except (OSError, ValueError, TypeError):
                    self.error = True
            return {"revision": self.memory["revision"],
                    "items": [{k: row[k] for k in ("key", "target", "scope")}
                              for row in self.memory["items"].values()],
                    "error": "待生效记录无法保存或读取，请勿据此判断配置已生效。" if self.error else ""}

    @staticmethod
    def _apply(data, operation):
        key, target, scope, changed, before, after = operation
        identity = target + ":" + key + ":" + scope
        previous = data["items"].get(identity)
        if changed and before == after:
            return
        baseline = previous.get("before", before) if previous else before
        if changed and baseline == after:
            data["items"].pop(identity, None)
        else:
            row = {"key": key, "target": target, "scope": scope}
            if changed:
                row.update(before=baseline, after=after)
            data["items"][identity] = row
        data["revision"] += 1

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix("." + secrets.token_hex(8) + ".tmp")
        try:
            staging.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
            staging.replace(self.path)
        finally:
            staging.unlink(missing_ok=True)

    def mark(self, key, *, target="chatgpt", scope=""):
        return self._change(key, target, scope, False, None, None)

    def _flush(self):
        with file_lock(self.path.with_suffix(".lock"), blocking=False):
            data = self._read()
            for value in self.pending:
                self._apply(data, value)
            self._write(data)
            self.memory, self.pending, self.error = data, [], False

    def change(self, key, before, after):
        if key not in VALUE_KEYS or any(type(v) not in {int, str, bool, type(None)} for v in (before, after)):
            raise ValueError("invalid restart notice value")
        return self._change(key, "chatgpt", "", True, before, after)

    def _change(self, key, target, scope, changed, before, after):
        if key not in KEYS or target not in {"chatgpt", "wsl"} or not isinstance(scope, str) or len(scope) > 160:
            raise ValueError("invalid restart notice item")
        if changed and before == after:
            return self.snapshot()
        operation = (key, target, scope, changed, before, after)
        with self.guard:
            self.pending.append(operation)
            try:
                self._flush()
            except (OSError, ValueError, TypeError):
                self._apply(self.memory, operation)
                self.error = True
            return self.snapshot()

    def acknowledge(self, target, revision, *, port_pending=False):
        if target not in {"chatgpt", "wsl"} or type(revision) is not int:
            raise ValueError("invalid restart notice acknowledgement")
        with self.guard:
            if self.pending:
                try:
                    self._flush()
                except (OSError, ValueError, TypeError) as exc:
                    raise ValueError("待生效记录尚未保存，请稍后重试。") from exc
            try:
                with file_lock(self.path.with_suffix(".lock"), blocking=False):
                    data = self._read()
                    if revision != data["revision"]:
                        raise ValueError("配置又有变更，请确认最新待生效项目后重试。")
                    if target == "chatgpt" and port_pending:
                        raise ValueError("请先重启请求优化器应用新端口，再重启 ChatGPT App。")
                    data["items"] = {k: v for k, v in data["items"].items() if v["target"] != target}
                    data["revision"] += 1
                    self._write(data)
                    self.memory, self.error = data, False
            except OSError as exc:
                raise ValueError("待生效记录尚未保存，请稍后重试。") from exc
            return self.snapshot()
