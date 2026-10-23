"""Install a self-contained stdio image tool; never execute image analysis."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil

OWNER = "codex-packet-inspector/isolated-image-tool"
ASSETS = {"server.ps1": "isolated_image_tool.ps1", "compress_image.ps1": "compress_image.ps1"}


def find_powershell():
    command = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not command:
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = str(candidate) if candidate.is_file() else None
    if not command:
        raise ValueError("未找到 PowerShell，无法安装独立图片工具。")
    return command


class ImageToolInstallation:
    def __init__(self, codex_home):
        self.codex_home = Path(codex_home).resolve()
        self.directory = self.codex_home / "tools" / "codex-inspector-image-tool"
        self.manifest = self.directory / "installation.json"

    @staticmethod
    def _write(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        stage = path.with_name(path.name + ".new")
        try:
            stage.write_bytes(raw)
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)

    def _owned(self):
        if self.directory.is_symlink() or self.directory.is_junction() or self.manifest.is_symlink():
            raise ValueError("独立图片工具目录不能是链接。")
        if not self.manifest.exists():
            if any((self.directory / name).exists() for name in ASSETS):
                raise ValueError("图片工具文件已存在且不属于本程序，未覆盖。")
            return {}
        state = json.loads(self.manifest.read_text(encoding="utf8"))
        if state.get("owner") != OWNER or set(state.get("files", {})) != set(ASSETS):
            raise ValueError("图片工具安装记录不匹配，未修改。")
        for name, digest in state["files"].items():
            path = self.directory / name
            if path.is_symlink() or (path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise ValueError("独立图片工具文件已被修改，未覆盖或删除。")
        return state

    def registration(self, cli):
        return {"command": find_powershell(),
                "args": ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(self.directory / "server.ps1")],
                "env": {"CODEX_HOME": str(self.codex_home), "CODEX_CLI_PATH": str(cli)},
                "tool_timeout_sec": 420}

    def install(self, cli):
        self._owned()
        registration = self.registration(cli)
        digests = {}
        for name, source in ASSETS.items():
            raw = Path(__file__).with_name(source).read_bytes()
            digests[name] = hashlib.sha256(raw).hexdigest()
            target = self.directory / name
            if not target.exists() or target.read_bytes() != raw:
                self._write(target, raw)
        self._write(self.manifest, json.dumps({"owner": OWNER, "schema": 1, "files": digests}, indent=2).encode())
        return registration

    def matches(self, registration):
        return bool(isinstance(registration, dict) and registration.get("command")
                    and str(self.directory / "server.ps1") in registration.get("args", [])
                    and self._owned())

    def uninstall(self):
        if not self._owned():
            return
        for name in (*ASSETS, self.manifest.name):
            (self.directory / name).unlink(missing_ok=True)

    @contextmanager
    def transaction(self):
        self._owned()
        paths = [self.directory / name for name in (*ASSETS, self.manifest.name)]
        previous = {path: path.read_bytes() if path.exists() else None for path in paths}
        try:
            yield
        except BaseException:
            for path, raw in previous.items():
                if raw is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != raw:
                    self._write(path, raw)
            raise
        finally:
            if self.directory.is_dir() and not any(self.directory.iterdir()):
                self.directory.rmdir()
