"""Opt-in frontend development settings stored only on the developer's machine."""
from __future__ import annotations

import json
from pathlib import Path
import secrets

CONFIG_NAME = "frontend-development.json"
RESOURCES = ("index.html", "i18n.js", "app.js", "style.css")


def _directory(value, enabled):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("本地前端目录无效，请重新配置开发模式。")
    directory = Path(value).resolve()
    if enabled and not all((directory / name).is_file() for name in RESOURCES):
        raise ValueError("本地前端目录不可用，请重新配置开发模式。")
    return directory


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def register(data_dir, ui_dir, enabled):
    """Explicit local setup; neither builds nor ordinary app startup create this."""
    if type(enabled) is not bool:
        raise ValueError("invalid_development_setting")
    directory = _directory(str(Path(ui_dir).resolve()), enabled)
    path = Path(data_dir) / CONFIG_NAME
    _save(path, {"enabled": enabled, "ui_dir": str(directory)})
    return path


class FrontendDevelopment:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.available = bool(self.path and self.path.is_file())
        self.enabled = False
        self.directory = None
        self.error = ""
        if self.available:
            try:
                value = self._read()
                self.directory = _directory(value.get("ui_dir"), value["enabled"])
                self.enabled = value["enabled"]
            except (OSError, ValueError, TypeError):
                self.error = "本地前端配置无效，已关闭开发模式。"

    def _read(self):
        if not self.available:
            raise ValueError("本机未配置前端开发模式。")
        if self.path.stat().st_size > 8192:
            raise ValueError("invalid_development_configuration")
        value = json.loads(self.path.read_text(encoding="utf8"))
        if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise ValueError("invalid_development_configuration")
        return value

    def status(self):
        return {"available": self.available, "enabled": self.enabled, "error": self.error}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid_development_setting")
        value = self._read()
        directory = _directory(value.get("ui_dir"), enabled)
        _save(self.path, {**value, "enabled": enabled})
        self.enabled, self.directory, self.error = enabled, directory, ""
        return self.status()
