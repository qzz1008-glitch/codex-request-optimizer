"""Standard-library operations executed as the connected Linux account."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time


LINUX_IMAGE_DATA_DIR = "~/.cache/codex-request-optimizer"
LINUX_IMAGE_NAME = re.compile(r"i-[a-f0-9]{30}\.(?:png|jpg|webp|gif)\Z")
LINUX_IMAGE_TTL = 7 * 24 * 3600
LINUX_IMAGE_MAX = 50 * 1024 * 1024
LINUX_TOOL_OWNER = "codex-packet-inspector/linux-image-tool"


def linux_write_private(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".image-", delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def linux_private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("图片目录需由当前 Linux 账号所有，且仅该账号可访问。")
    return path.resolve()


def linux_image_directory(value):
    path = Path(value or LINUX_IMAGE_DATA_DIR).expanduser()
    if not path.is_absolute():
        raise ValueError("数据目录须为 Linux 绝对路径或以 ~/ 开头。")
    return linux_private_directory(path / "images")


def linux_image_store(directory, entries, referenced):
    confirmed = []
    now = time.time()
    for name, entry in entries.items():
        if not LINUX_IMAGE_NAME.fullmatch(name) or not 0 < entry["size"] <= LINUX_IMAGE_MAX:
            raise ValueError("图片缓存条目无效。")
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if (info and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size == entry["size"]):
            os.utime(path, None, follow_symlinks=False)
        elif "data" in entry:
            data = base64.b64decode(entry["data"], validate=True)
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest()[:30] != name[2:32]:
                raise ValueError("图片上传校验失败。")
            linux_write_private(path, data)
        else:
            continue
        confirmed.append(name)

    # Keep active references alive and make expiry independent of requests to
    # the Windows UI. Only our own filenames in this directory are eligible.
    protected = set(entries) | set(referenced)
    for path in directory.iterdir():
        if not LINUX_IMAGE_NAME.fullmatch(path.name):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                continue
            if path.name in referenced and path.name not in entries:
                os.utime(path, None, follow_symlinks=False)
            elif path.name not in protected and max(info.st_atime, info.st_mtime) < now - LINUX_IMAGE_TTL:
                path.unlink()
        except FileNotFoundError:
            continue
    return {"confirmed": confirmed, "directory": str(directory)}


def linux_tool_files(home):
    folder = home / "tools" / "codex-inspector-image-tool"
    script, manifest = folder / "server.py", folder / "linux-installation.json"
    if any(path.is_symlink() for path in (folder, script, manifest)):
        raise ValueError("Linux 图片工具目录或文件不能是链接。")
    if not manifest.exists() and script.exists():
        raise ValueError("Linux 图片工具文件已存在，未覆盖。")
    previous = script.read_bytes() if script.exists() else None
    if manifest.exists():
        state = json.loads(manifest.read_text(encoding="utf-8"))
        if state.get("owner") != LINUX_TOOL_OWNER or (previous is not None and
                state.get("sha256") != hashlib.sha256(previous).hexdigest()):
            raise ValueError("Linux 图片工具已被修改，已保留现有文件。")
    return script, manifest, previous


def linux_image_runtime(home, mode, source):
    install_dir = os.environ.get("CODEX_INSTALL_DIR") or str(Path.home() / ".local/bin")
    cli = os.environ.get("CODEX_CLI_PATH")
    if not cli or not Path(cli).is_absolute() or not os.access(cli, os.X_OK):
        cli = shutil.which("codex", path=install_dir + os.pathsep + os.environ.get("PATH", ""))
    if not cli or not Path(cli).is_absolute() or not os.access(cli, os.X_OK):
        raise ValueError("远端未找到可执行的 Codex CLI，图片工具配置未更改。")
    environment = {key: value for key, value in os.environ.items() if key not in {
        "CODEX_CONFIG_FILE", "CODEX_CONFIG_PATH", "CODEX_TEST_USER_CONFIG_FILE",
        "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_TOKEN"}}
    with tempfile.TemporaryDirectory(prefix="codex-image-capabilities-") as folder:
        try:
            result = subprocess.run([cli, "-c", "features.view_image=false", "features", "list"],
                env={**environment, "CODEX_HOME": folder}, cwd=folder, capture_output=True,
                text=True, errors="replace", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("远端 Codex 图片工具能力检测失败。") from None
    for line in result.stdout.splitlines():
        fields = line.split()
        if (not result.returncode and len(fields) >= 2 and fields[0] == "view_image"
                and fields[-1] == "false" and "removed" not in fields[1:-1]):
            break
    else:
        raise ValueError("远端 Codex 未提供可验证的图片工具开关，原配置已保留。")
    if mode == "disabled":
        return {}
    python = shutil.which("python3")
    if not python:
        raise ValueError("远端未找到 Python 3，图片工具未安装。")
    tool, manifest, previous = linux_tool_files(home)
    linux_private_directory(tool.parent)
    code = source.encode("utf-8")
    compile(code, str(tool), "exec")
    registration = {"command": python, "args": ["-I", str(tool)], "tool_timeout_sec": 420,
                    "env": {"CODEX_HOME": str(home), "CODEX_CLI_PATH": cli}}
    with tempfile.TemporaryDirectory(prefix="codex-image-tool-check-") as folder:
        config = ["[mcp_servers.isolated_vision]",
                  *(key + "=" + json.dumps(value, ensure_ascii=False)
                    for key, value in registration.items() if key != "env"),
                  "[mcp_servers.isolated_vision.env]",
                  *(key + "=" + json.dumps(value, ensure_ascii=False) for key, value in registration["env"].items())]
        (Path(folder) / "config.toml").write_text("\n".join(config) + "\n", encoding="utf-8")
        try:
            result = subprocess.run([cli, "mcp", "get", "isolated_vision", "--json"],
                env={**environment, "CODEX_HOME": folder}, cwd=folder, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("远端图片工具配置验证失败，原配置已保留。") from None
        if result.returncode:
            raise ValueError("远端 Codex 未接受图片工具配置，原配置已保留。")
    if code != previous:
        linux_write_private(tool, code)
        try:
            state = {"owner": LINUX_TOOL_OWNER, "sha256": hashlib.sha256(code).hexdigest()}
            linux_write_private(manifest, json.dumps(state).encode("utf-8"))
        except OSError:
            if previous is None:
                tool.unlink(missing_ok=True)
            else:
                linux_write_private(tool, previous)
            raise
    return registration


def linux_image_control(home, options):
    action = options["action"].removeprefix("tunnel_")
    if action == "image_tool":
        return linux_image_runtime(home, options["mode"], options.get("source", ""))
    if action == "image_uninstall":
        tool, manifest, _ = linux_tool_files(home)
        tool.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        return {}
    directory = linux_image_directory(options.get("data_dir"))
    if action == "image_probe":
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"ok")
        return {"directory": str(directory)}
    if action == "image_store":
        return linux_image_store(directory, options["entries"], set(options["referenced"]))
    raise ValueError("无效的 Linux 图片操作。")
