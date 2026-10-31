"""Codex desktop updates must not strand consumers on removed runtime paths."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.codex_runtime import find_codex_cli
from packet_inspector.sharing_auth import native_command, SharingError


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "CODEX_CLI_PATH": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def executable(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic executable placeholder", encoding="ascii")
        return path.resolve()

    def test_removed_explicit_path_uses_valid_environment_for_both_consumers(self):
        available = self.executable("current/codex.exe")
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(available)}), patch(
                "packet_inspector.codex_runtime.shutil.which", side_effect=AssertionError("valid hint wins")):
            self.assertEqual(find_codex_cli(self.root / "removed/codex.exe"), str(available))
            self.assertEqual(native_command()[0], str(available))

    def test_removed_environment_path_does_not_hide_path_installation(self):
        available = self.executable("path/codex.exe")
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.root / "removed.exe")}), patch(
                "packet_inspector.codex_runtime.shutil.which", return_value=str(available)):
            self.assertEqual(find_codex_cli(), str(available))

    def test_stale_hints_and_one_unreadable_runtime_do_not_hide_desktop_update(self):
        old = self.executable("OpenAI/Codex/bin/old/codex.exe")
        new = self.executable("OpenAI/Codex/bin/new/codex.exe")
        os.utime(old, ns=(1_000_000_000, 1_000_000_000))
        os.utime(new, ns=(2_000_000_000, 2_000_000_000))
        original = Path.stat

        def stat(path, *args, **kwargs):
            if path == old:
                raise PermissionError("synthetic obsolete runtime access failure")
            return original(path, *args, **kwargs)

        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.root / "missing.exe")}), patch(
                "packet_inspector.codex_runtime.shutil.which", return_value=None), patch.object(Path, "stat", stat):
            self.assertEqual(find_codex_cli("\x00invalid"), str(new))

    def test_directory_is_not_an_executable_and_missing_runtime_is_explicit(self):
        folder = self.root / "OpenAI/Codex/bin/invalid/codex.exe"
        folder.mkdir(parents=True)
        with patch("packet_inspector.codex_runtime.shutil.which", return_value=None):
            self.assertIsNone(find_codex_cli(folder))
            with self.assertRaises(SharingError):
                native_command()

    def test_configured_command_name_is_resolved_by_path(self):
        available = self.executable("custom/codex-local.exe")
        with patch("packet_inspector.codex_runtime.shutil.which", side_effect=lambda name:
                   str(available) if name == "codex-local" else None):
            self.assertEqual(find_codex_cli("codex-local"), str(available))
