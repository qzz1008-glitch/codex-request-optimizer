"""Locate the installed Codex CLI without pinning a desktop runtime revision."""
import os
from pathlib import Path
import shutil


def find_codex_cli(configured=None):
    """Prefer valid explicit paths, then PATH and the desktop's versioned cache.

    Saved paths and inherited environment variables can outlive a desktop
    update. A removed executable must not hide another installed runtime.
    Discovery never executes candidates or changes the Codex installation.
    """
    for selected in (configured, os.environ.get("CODEX_CLI_PATH")):
        if not isinstance(selected, (str, os.PathLike)) or not selected:
            continue
        try:
            if Path(selected).is_file():
                return str(Path(selected).resolve())
            if str(selected) == Path(selected).name and "\x00" not in str(selected):
                executable = shutil.which(selected)
                if executable:
                    return executable
        except (OSError, ValueError):
            continue
    executable = shutil.which("codex") or shutil.which("codex.exe")
    if executable:
        return executable
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    candidates = []
    try:
        for path in (Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"):
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime_ns, str(path.resolve())))
            except OSError:
                continue
    except OSError:
        pass
    return max(candidates)[1] if candidates else None
