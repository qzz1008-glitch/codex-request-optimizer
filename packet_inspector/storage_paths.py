"""One storage root with grouped data; legacy locations remain readable in place."""
import os
from pathlib import Path
import tempfile


def resolve_directory(data_dir, value=""):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("目录必须是文本路径")
    directory = Path(os.path.expandvars(value.strip())).expanduser() if value.strip() else Path(data_dir)
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


def grouped_paths(data_dir, value=""):
    directory = resolve_directory(data_dir, value)
    return {"storage_dir": "" if directory == Path(data_dir).resolve() else str(directory),
            "log_dir": str(directory / "logs"), "cache_dir": str(directory / "cache"),
            "image_save_dir": str(directory / "images")}


def path_settings(data_dir, data):
    if "storage_dir" in data:
        if {"log_dir", "cache_dir", "image_save_dir"}.intersection(data):
            raise ValueError("数据目录不能与独立目录同时设置。")
        result = grouped_paths(data_dir, data["storage_dir"])
        for key in ("log_dir", "cache_dir", "image_save_dir"):
            check_directory(Path(result[key]))
        return result
    result = {}
    for key in ("log_dir", "cache_dir"):
        if key in data:
            directory = resolve_directory(data_dir, data[key])
            check_directory(directory)
            result[key] = "" if directory == Path(data_dir).resolve() else str(directory)
    if result:
        result["storage_dir"] = None  # Older clients may still save independent paths.
    return result
