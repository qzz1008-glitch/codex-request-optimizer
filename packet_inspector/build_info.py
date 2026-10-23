"""Small, non-sensitive identity of the code embedded in this executable."""
from __future__ import annotations

import json
from pathlib import Path
import re


def build_info(path=None):
    manifest = Path(path) if path is not None else Path(__file__).with_name("_source_manifest.json")
    try:
        with manifest.open(encoding="utf8") as stream:
            text = stream.read(256 * 1024 + 1)
        if len(text) > 256 * 1024:
            return {}
        value = json.loads(text)
        if not isinstance(value, dict):
            return {}
        if (not re.fullmatch(r"[a-f0-9]{40}", str(value.get("git_commit", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("source_sha256", "")))
                or not re.fullmatch(r"\d+\.\d+\.\d+", str(value.get("version", "")))
                or value.get("mode") not in ("dev", "release")):
            return {}
        return {key: value[key] for key in ("git_commit", "source_sha256", "version", "mode", "dirty")
                if key in value}
    except (OSError, ValueError):
        return {}
