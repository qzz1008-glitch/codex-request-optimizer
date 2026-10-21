"""Per-user Windows login startup for the installed application."""
import os
from pathlib import Path
import sys

from .installation import manage_installation


def startup_supported(data_dir):
    folder = Path(data_dir).resolve()
    executable = Path(sys.executable).resolve()
    installed = folder / "bin" / "CodexPacketInspector.exe"
    return (os.name == "nt" and bool(getattr(sys, "frozen", False))
            and folder == (Path.home() / ".codex-packet-inspector").resolve()
            and (executable == installed or (installed.is_file() and executable.is_relative_to(folder / "bin" / "versions"))))


def configure_startup(enabled, data_dir, config, port):
    if not startup_supported(data_dir):
        raise ValueError("开机启动功能请使用已安装的 Windows EXE。")
    return manage_installation("EnableStartup" if enabled else "DisableStartup", data_dir, config, port)
