"""Keep the EXE the user originally ran current after a settings update."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from .managed_runtime import sha256_file
from .router import atomic_json
from .updates import ASSET, version_tuple


def _read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_settings_download(directory, source, checksum, version):
    state = _read_state(directory / "update-state.json")
    try:
        release = state["latest_version"]
        return (state["state"] == "upgrading" and version_tuple(release) == version
                and source == directory / "updates" / release / ASSET
                and state["asset"]["sha256"] == checksum)
    except (KeyError, TypeError, ValueError):
        return False


class DownloadedLauncher:
    def __init__(self, directory, config, source, checksum, *, from_settings=False):
        self.directory, self.config = directory, str(config)
        self.source, self.checksum, self.from_settings = source, checksum, from_settings
        self.record_path = directory / "downloaded-launcher.json"

    def _external_exe(self, value):
        if not isinstance(value, (str, Path)):
            return None
        path = Path(value)
        if (path.is_absolute() and path.suffix.lower() == ".exe" and not path.is_relative_to(self.directory)
                and path.resolve() == path and not path.is_symlink() and path.is_file()):
            return path
        return None

    def _record(self):
        record = _read_state(self.record_path)
        return record if record.get("config") == self.config else {}

    def _save(self, path, checksum):
        atomic_json(self.record_path, {"config": self.config, "path": str(path), "sha256": checksum})

    def remember(self, source, checksum):
        try:
            if path := self._external_exe(source):
                self._save(path, checksum)
        except (OSError, RuntimeError, ValueError):
            pass  # Recording a launch path must not prevent an ordinary launch.

    def recover(self, installed_payload):
        if installed_payload is None or self._record():
            return
        # Older versions reported their launch source but did not retain it across updates.
        previous = _read_state(self.directory / "launch-status.json")
        if (previous.get("state") == "complete" and previous.get("action") in {"upgrade", "open_installed"}
                and previous.get("source_sha256") == installed_payload.parent.name):
            self.remember(previous.get("source_executable"), previous["source_sha256"])

    def after_install(self, verified_package):
        if not self.from_settings:
            self.remember(self.source, self.checksum)
            return None

        original = self._record()
        if not original:
            return "untracked"
        temporary = None
        try:
            path = self._external_exe(original.get("path"))
            if path is None or sha256_file(path) != original.get("sha256"):
                return "changed"
            with tempfile.NamedTemporaryFile(prefix=".optimizer-update-", suffix=".tmp", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                with verified_package.open("rb") as source:
                    shutil.copyfileobj(source, stream)
            if sha256_file(temporary) != self.checksum:
                return "verification_failed"
            if self._external_exe(path) is None or sha256_file(path) != original["sha256"]:
                return "changed"
            temporary.replace(path)
            self.remember(path, self.checksum)
            return "updated"
        except (OSError, RuntimeError, ValueError):
            return "unavailable"
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
