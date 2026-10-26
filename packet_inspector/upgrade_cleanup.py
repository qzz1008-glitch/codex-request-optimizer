"""Bound managed application copies without touching shared captures or settings."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

from .frontend_dev import installed_profile
from .router import process_alive, worker_info

GRACE_SECONDS = 7 * 86400
ROLLBACK_COPIES = 2


def _json(path):
    if path.stat().st_size > 65536:
        raise ValueError("upgrade metadata too large")
    value = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(value, dict):
        raise ValueError("invalid upgrade metadata")
    return value


def cleanup_versions(data_dir, *, now=None):
    """Fail closed on unclear ownership; cleanup failures never undo an upgrade."""
    directory = Path(data_dir).resolve()
    versions = directory / "bin" / "versions"
    result = {"versions_removed": 0, "worker_records_removed": 0, "downloads_removed": 0}
    if not versions.exists():
        return result
    try:
        if versions.resolve() != versions or versions.is_symlink() or versions.is_junction():
            raise ValueError("version directory is redirected")
        records, protected = [], set()

        def protect(executable):
            path = Path(executable).resolve()
            if path.is_relative_to(versions):
                protected.add(versions / path.relative_to(versions).parts[0])

        def protect_routes():
            state = _json(directory / "router-state.json")
            identities = [state["active"], *[entry["id"] for entry in state.get("draining", [])]]
            for identity in identities:
                protect(worker_info(directory, identity)["executable"])

        protect_routes()
        profile = installed_profile(directory)
        if profile.get("executable"):
            protect(profile["executable"])
        workers = directory / "router-workers"
        for index, path in enumerate(workers.glob("*.json")):
            if index >= 4096:
                raise ValueError("too many worker records")
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            worker = worker_info(directory, path.stem)
            runtime = path.with_name(path.stem + ".runtime.json")
            alive = False
            if runtime.exists():
                pid = _json(runtime).get("pid")
                if type(pid) is not int or pid <= 0:
                    raise ValueError("invalid worker runtime")
                alive = process_alive(pid)
            if alive:
                protect(worker["executable"])
            records.append((path, runtime, Path(worker["executable"]).resolve().parent, alive))
        candidates = []
        for path in versions.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", path.name):
                continue
            if path.is_symlink() or path.is_junction() or path.resolve().parent != versions:
                continue
            if path.is_dir():
                candidates.append(path)
        # Keep two additional versions for manual recovery, plus every live owner.
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        retained = set([path for path in candidates if path not in protected][:ROLLBACK_COPIES])
        cutoff = (time.time() if now is None else now) - GRACE_SECONDS
        for path in candidates:
            if path in protected or path in retained or path.stat().st_mtime >= cutoff:
                continue
            protect_routes()  # A draining owner may have changed during the scan.
            if path in protected:
                continue
            contents = list(path.iterdir())
            if not contents or any(item.name not in {"CodexPacketInspector.exe", "CodexPacketInspector.exe.new"}
                                   or item.is_symlink() or not item.is_file() for item in contents):
                continue
            try:
                for item in contents:
                    item.unlink()
                path.rmdir()
            except OSError:
                continue  # Windows may still have an EXE or a loader open.
            result["versions_removed"] += 1
            for descriptor, runtime, owner, alive in records:
                if owner == path and not alive:
                    try:
                        runtime.unlink(missing_ok=True)
                        descriptor.unlink(missing_ok=True)
                        result["worker_records_removed"] += 1
                    except OSError:
                        pass
        result["downloads_removed"] = _cleanup_downloads(directory, cutoff)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result["skipped"] = type(exc).__name__
    return result


def _cleanup_downloads(directory, cutoff):
    from .updates import ASSET, LEGACY_ASSET
    downloads = directory / "updates"
    if not downloads.exists():
        return 0
    if downloads.resolve() != downloads or downloads.is_symlink() or downloads.is_junction():
        raise ValueError("download directory is redirected")
    folders = [path for path in downloads.iterdir()
               if re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", path.name)
               and path.is_dir() and not path.is_symlink() and not path.is_junction()
               and path.resolve().parent == downloads]
    folders.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    executable = Path(sys.executable).resolve()
    removed = 0
    for path in folders[ROLLBACK_COPIES:]:
        if executable.is_relative_to(path):
            continue
        contents = list(path.iterdir())
        allowed = {ASSET, LEGACY_ASSET, ASSET + ".download", LEGACY_ASSET + ".download"}
        if any(item.name not in allowed or item.is_symlink() or not item.is_file() for item in contents):
            continue
        if max([path.stat().st_mtime, *[item.stat().st_mtime for item in contents]]) >= cutoff:
            continue
        try:
            for item in contents:
                item.unlink()
            path.rmdir()
            removed += 1
        except OSError:
            pass
    return removed
