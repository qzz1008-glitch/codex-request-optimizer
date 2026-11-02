"""Explicit, reversible configuration work for an application upgrade.

Keep migrations here rather than in standby worker construction. Each migration
owns its files and rolls back only its own writes, preserving concurrent edits.
The caller keeps this transaction open until the service handover is verified.
"""
from contextlib import contextmanager
import json

from .config_link import ConfigLink
from .image_config import ImageToolConfig, image_mode
from .restart_notice import RestartNotices


@contextmanager
def configuration_upgrade(data_dir, config):
    settings_path = data_dir / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf8")) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("升级配置无效，已保留当前服务。")
    result = {"image_tools": {"state": "skipped"}}
    # An upgrade must never enable an optional feature or change its mode.
    if (data_dir / "image-tool.json").exists() and image_mode(settings) in {"disabled", "optimized"}:
        manager = ImageToolConfig(ConfigLink(config, data_dir / "codex-link.json"), data_dir, lambda: "")
        mode = image_mode(settings)
        if manager.mode() == mode and (mode == "optimized" or manager.needs_native_refresh()):
            with manager.change(mode) as changed:
                result["image_tools"] = {"state": "complete", "changed": bool(changed)}
                yield result
            if changed:
                RestartNotices(data_dir, config).mark("image_analysis_mode")
            return
    yield result
