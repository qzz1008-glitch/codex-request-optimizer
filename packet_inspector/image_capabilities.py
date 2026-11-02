"""Verify native image configuration by capabilities, never a version range."""
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import tempfile

from .codex_runtime import find_codex_cli

IMAGE_FEATURE = "features.view_image"


def image_runtime_identity(configured=None):
    cli = find_codex_cli(configured)
    if not cli:
        raise ValueError("未找到 Codex 内核，无法确认图片工具开关；原配置已保留。")
    try:
        path = Path(cli).resolve()
        info = path.stat()
        return {"cli": str(path), "size": info.st_size, "modified_ns": info.st_mtime_ns}
    except OSError:
        raise ValueError("无法读取 Codex 内核，未修改图片工具配置。") from None


def verify_view_image_feature(configured=None):
    identity = image_runtime_identity(configured)
    return _verify_feature(identity["cli"], identity["size"], identity["modified_ns"])


@lru_cache(maxsize=8)
def _verify_feature(cli, size, modified_ns):
    # Isolate the probe from account credentials, project layers and config
    # errors. This command lists local capabilities; it never requests a model.
    with tempfile.TemporaryDirectory(prefix="codex-image-capabilities-") as folder:
        environment = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "CODEX_CONFIG_FILE",
                    "CODEX_CONFIG_PATH", "CODEX_TEST_USER_CONFIG_FILE"):
            environment.pop(key, None)
        environment["CODEX_HOME"] = folder
        try:
            result = subprocess.run([cli, "-c", IMAGE_FEATURE + "=false", "features", "list"],
                env=environment, cwd=folder, capture_output=True, text=True, encoding="utf8",
                errors="replace", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("无法读取 Codex 的图片工具能力；原配置已保留，请确认内核可运行后重试。") from None
        if result.returncode:
            raise ValueError("Codex 未完成图片工具能力检测；原配置已保留，请检查内核后重试。")
        for line in result.stdout.splitlines():
            fields = line.split()
            if (len(fields) >= 2 and fields[0] == "view_image" and fields[-1] == "false"
                    and "removed" not in fields[1:-1]):
                return IMAGE_FEATURE
        raise ValueError("当前 Codex 内核未提供可验证的图片工具开关，未修改配置。")
