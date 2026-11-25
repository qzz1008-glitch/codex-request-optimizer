"""Install the Windows runtime once inside the verified version directory."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
from tempfile import TemporaryDirectory


APP_EXE = "CodexPacketInspector.exe"
RUNTIME_DIRECTORY = "runtime"
LIBRARIES = "_internal"
EMBEDDED_RUNTIME = "packet_inspector/_managed"
LAYOUT_FILE = "layout.json"


def sha256_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _valid_resource(name):
    return all(part and part not in {".", ".."} and not part.endswith((".", " "))
               and not re.search(r'[<>:"\\|?*\x00-\x1f]', part) and not PureWindowsPath(part).is_reserved()
               for part in name.split("/"))


def read_layout(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Windows runtime layout is too large")
    value = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("files"), dict):
        raise ValueError("Invalid Windows runtime layout")
    files = value["files"]
    if (not files or len({key.casefold() for key in files}) != len(files)
            or any(not _valid_resource(key) for key in files)
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for digest in [value.get("exe"), *files.values()])):
        raise ValueError("Invalid Windows runtime files")
    return value


def _runtime_files(directory, layout):
    return {directory / APP_EXE: layout["exe"],
            **{directory / LIBRARIES / name: digest for name, digest in layout["files"].items()}}


def _fits_windows_paths(paths):
    # Directory creation also needs room for an 8.3 name. Use the traditional
    # limits so the installed layout works without the LongPathsEnabled policy.
    def units(path):
        return len(str(path).encode("utf-16-le")) // 2

    return all(units(path) < 260 and units(path.parent) <= 248 for path in paths)


def _verify(files, *, allow_removed=False):
    for path, expected in files.items():
        if path.resolve() != path or path.is_symlink() or path.is_junction():
            raise ValueError("Windows runtime file is redirected")
        if allow_removed and not path.exists():
            continue
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError("Windows runtime file failed verification: " + path.name)


def embedded_resources():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    root = Path(sys._MEIPASS)
    return root if (root / EMBEDDED_RUNTIME / LAYOUT_FILE).is_file() else None


def application_payload(executable, data_dir):
    path = Path(executable).resolve()
    version = path.parent.parent
    if (path.name == APP_EXE and path.parent.name == RUNTIME_DIRECTORY
            and version.parent == (Path(data_dir) / "bin" / "versions").resolve()
            and re.fullmatch(r"[0-9a-f]{64}", version.name)):
        return version / APP_EXE
    return path


def install_runtime(application, resources=None):
    """Keep the self-contained application for compatibility and manual repair."""
    runtime = application.parent.resolve() / RUNTIME_DIRECTORY
    layout_path = None if resources is None else resources / EMBEDDED_RUNTIME / LAYOUT_FILE
    if runtime.exists() or runtime.is_symlink() or runtime.is_junction():
        if runtime.resolve() != runtime or not runtime.is_dir():
            raise ValueError("Windows runtime directory is redirected")
        layout = read_layout(runtime / LAYOUT_FILE)
        if layout_path is not None and layout != read_layout(layout_path):
            raise ValueError("Windows runtime does not match this application")
        _verify(_runtime_files(runtime, layout))
    elif layout_path is None:
        return application
    else:
        layout = read_layout(layout_path)
        with TemporaryDirectory(prefix=".r-", dir=runtime.parent) as staging:
            target = Path(staging)
            files = _runtime_files(target, layout)
            # The staging name is longer than the final one. The original
            # single-file application also supports deeper installation paths.
            if sys.platform == "win32" and not _fits_windows_paths(files):
                return application
            shutil.copyfile(layout_path.with_name(APP_EXE), target / APP_EXE)
            for name in layout["files"]:
                destination = target / LIBRARIES / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resources / name, destination)
            _verify(files)
            shutil.copyfile(layout_path, target / LAYOUT_FILE)
            target.rename(runtime)
    return runtime / APP_EXE


def remove_runtime(directory):
    """Prune only unchanged installer files. A live runtime keeps its EXE locked."""
    try:
        marker = directory / LAYOUT_FILE
        if marker.resolve() != marker or marker.is_symlink():
            return False
        files = _runtime_files(directory, read_layout(marker))
        folders = {directory}
        for path in files:
            folders.update(parent for parent in path.parents if parent.is_relative_to(directory))
        owned = {*folders, *files, marker}
        for folder in folders:
            if folder.resolve() != folder or folder.is_symlink() or folder.is_junction():
                return False
            if folder.exists() and any(child not in owned for child in folder.iterdir()):
                return False
        _verify(files, allow_removed=True)
        for path in files:
            path.unlink(missing_ok=True)  # The EXE must be released before deleting its resources.
        for folder in sorted(folders - {directory}, key=lambda path: len(path.parts), reverse=True):
            if folder.exists():
                folder.rmdir()
        marker.unlink()
        directory.rmdir()
        return True
    except (OSError, ValueError):
        return False
