"""Per-user Windows installation, independent of Codex and Python installations."""
import json
import os
from pathlib import Path
import subprocess
import sys


def manage_installation(action, data_dir, config, port):
    supported = os.name == "nt" and getattr(sys, "frozen", False)
    # Disposable instances used for development/testing must not alter login tasks.
    supported = supported and Path(data_dir).resolve() == (Path.home() / ".codex-packet-inspector").resolve()
    if not supported:
        if action != "Status":
            raise ValueError("安装功能请使用正式 EXE 和默认数据目录。源码运行无需安装。")
        return {"supported": False, "installed": False}
    executable = Path(sys.executable).resolve()
    installed = Path(data_dir).resolve() / "bin" / "CodexPacketInspector.exe"
    if installed.is_file() and executable.is_relative_to(installed.parent / "versions"):
        executable = installed
    command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
               "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("windows_install.ps1")),
               "-Action", action, "-Executable", str(executable), "-DataDirectory", str(data_dir),
               "-ConfigPath", str(config), "-Port", str(port)]
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # The helper only handles local paths/tasks; it never receives credentials.
        raise ValueError("安装入口操作失败：" + (result.stderr.strip() or "Windows 返回错误")[-220:])
    return json.loads(result.stdout)
