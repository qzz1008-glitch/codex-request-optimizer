"""Configurable record/cache and diagnostic directories; settings stay in place."""
from pathlib import Path
import tempfile


def resolve_directory(data_dir, value=""):
    if not isinstance(value, str):
        raise ValueError("目录必须是文本路径")
    directory = Path(value.strip()).expanduser() if value.strip() else Path(data_dir)
    if not directory.is_absolute():
        raise ValueError("请填写绝对目录路径")
    if directory.exists() and not directory.is_dir():
        raise ValueError("所选路径不是目录")
    return directory.resolve()


def check_directory(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory, prefix=".inspector-write-check-") as file:
            file.write(b"ok")
            file.flush()
    except OSError as exc:
        raise ValueError("目录无法写入：" + type(exc).__name__) from None


def path_settings(data_dir, data):
    result = {}
    for key in ("log_dir", "cache_dir"):
        if key in data:
            directory = resolve_directory(data_dir, data[key])
            check_directory(directory)
            result[key] = "" if directory == Path(data_dir).resolve() else str(directory)
    return result
