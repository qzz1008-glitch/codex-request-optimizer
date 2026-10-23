"""Default build mode and the desktop profile retained across upgrades."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import shutil
import sys

from .build_info import build_info

PROFILE_NAME = "_build_profile.json"
RESOURCES = ("index.html", "i18n.js", "app.js", "style.css")
DESKTOP_PROFILE = "desktop-profile.json"
PORTABLE_UI = "@bundled"


@dataclass(frozen=True)
class BuildProfile:
    mode: str = "release"
    ui_dir: Path | None = None

    @property
    def development(self):
        return self.mode == "dev"


def validate_ui_dir(directory):
    directory = Path(directory)
    if not directory.is_absolute() or not all((directory / name).is_file() for name in RESOURCES):
        raise ValueError("开发版前端目录不可用，请在当前项目中重新构建开发版 EXE。")
    return directory.resolve()


def load_profile(path=None):
    """No local switch or environment variable can override the embedded profile."""
    path = Path(path) if path is not None else Path(__file__).with_name(PROFILE_NAME)
    if not path.exists():
        return BuildProfile()
    try:
        if path.stat().st_size > 8192:
            raise ValueError("build profile too large")
        data = json.loads(path.read_text(encoding="utf8"))
        if data == {"mode": "release"}:
            return BuildProfile()
        if data == {"mode": "dev", "ui_dir": PORTABLE_UI}:
            return BuildProfile("dev")
        if not isinstance(data, dict) or set(data) != {"mode", "ui_dir"} or data["mode"] != "dev":
            raise ValueError("invalid build profile")
        if not isinstance(data["ui_dir"], str):
            raise ValueError("invalid frontend directory")
        return BuildProfile("dev", validate_ui_dir(data["ui_dir"]))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("EXE 构建配置无效或开发目录不可用，请重新构建。") from exc


def installed_profile(data_dir):
    path = Path(data_dir) / DESKTOP_PROFILE
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 16384:
            raise ValueError("profile too large")
        data = json.loads(path.read_text(encoding="utf8"))
        if not isinstance(data, dict) or data.get("mode") not in {"dev", "release"}:
            raise ValueError("invalid mode")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("桌面运行模式记录无效，未切换运行模式。") from exc


def resolve_profile(profile, data_dir):
    """Keep the installed mode, with editable assets for portable dev builds."""
    previous = installed_profile(data_dir)
    mode = previous.get("mode", profile.mode)
    if mode == "release":
        return BuildProfile()
    external = previous.get("ui_dir") if not previous.get("managed_ui") else None
    if not previous and profile.ui_dir:
        external = profile.ui_dir
    if external:
        try:
            return BuildProfile("dev", validate_ui_dir(external))
        except ValueError:
            pass
    from . import VERSION
    identity = build_info()
    resource_version = VERSION + "-" + identity["source_sha256"][:16] if identity else VERSION
    directory = Path(data_dir).resolve() / "development" / "ui" / resource_version
    directory.mkdir(parents=True, exist_ok=True)
    for name in RESOURCES:
        target = directory / name
        if not target.exists():
            shutil.copy2(Path(__file__).with_name("ui") / name, target)
    return BuildProfile("dev", validate_ui_dir(directory))


def remember_profile(data_dir, profile):
    from .router import atomic_json
    directory = Path(data_dir).resolve()
    managed = bool(profile.ui_dir and profile.ui_dir.is_relative_to(directory / "development" / "ui"))
    data = {"mode": profile.mode, "ui_dir": str(profile.ui_dir) if profile.ui_dir else None,
            "managed_ui": managed, "pid": os.getpid(), "executable": str(Path(sys.executable).resolve())}
    atomic_json(directory / DESKTOP_PROFILE, data)


def remember_upgrade_profile(data_dir, default_profile, windows):
    """Old development EXEs predate the persistent desktop profile."""
    if installed_profile(data_dir):
        return
    from .router import atomic_json
    mode = "dev" if any(item.get("mode") == "dev" for item in windows) else "release" if windows else default_profile.mode
    external = default_profile.ui_dir if mode == "dev" else None
    if mode == "dev" and not external:
        for item in windows:
            if item.get("mode") != "dev" or not item.get("executable"):
                continue
            for parent in list(Path(item["executable"]).parents)[:8]:
                candidate = parent / "packet_inspector" / "ui"
                if all((candidate / name).is_file() for name in RESOURCES):
                    external = candidate.resolve()
                    break
            if external:
                break
    atomic_json(Path(data_dir) / DESKTOP_PROFILE,
                {"mode": mode, "ui_dir": str(external) if external else None, "managed_ui": external is None})
