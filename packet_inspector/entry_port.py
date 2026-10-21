"""Persistent entry address, separate from a running relay's mutable settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets

DEFAULT_PORT = 47844
CONFIG_NAME = "entry-port.json"


def validate_port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError("入口端口必须是 1024 到 65535 之间的整数。")
    return value


def configured_port(data_dir, fallback=DEFAULT_PORT):
    path = Path(data_dir) / CONFIG_NAME
    if not path.exists():
        return fallback
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("入口端口配置格式无效。")
    return validate_port(data.get("port"))


def port_status(data_dir, current_port):
    try:
        desired = configured_port(data_dir, current_port)
    except (OSError, ValueError, TypeError):
        return {"current_port": current_port, "configured_port": None,
                "pending": False, "error": "入口端口配置无效，请重新保存。"}
    return {"current_port": current_port, "configured_port": desired,
            "pending": desired != current_port}


def save_port(data_dir, current_port, value):
    value = validate_port(value)
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONFIG_NAME
    staging = directory / (CONFIG_NAME + "." + secrets.token_hex(8) + ".tmp")
    try:
        staging.write_text(json.dumps({"port": value}, indent=2) + "\n", encoding="utf8")
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return port_status(directory, current_port)


class ConfiguredPortParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        options, remaining = super().parse_known_args(args, namespace)
        if options.port is None:
            try:
                options.port = configured_port(options.data_dir)
            except (OSError, ValueError, TypeError) as exc:
                self.error("无法读取入口端口配置：" + str(exc))
        # --port 0 remains available for isolated development and verification.
        if not 0 <= options.port <= 65535:
            self.error("--port must be between 0 and 65535")
        return options, remaining
