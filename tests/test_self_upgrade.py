"""Main EXEs install themselves and preserve the installed desktop mode."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector import VERSION, launch
from packet_inspector.frontend_dev import (BuildProfile, DESKTOP_PROFILE, PORTABLE_UI, RESOURCES,
    installed_profile, load_profile, remember_upgrade_profile, resolve_profile)
from packet_inspector.upgrade import run_integrated


class SelfUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text('model="fixture"\n', encoding="utf8")
        self.executable = self.root / "CodexPacketInspector.exe"
        self.executable.write_bytes(b"new-application-fixture")
        self.arguments = ["--data-dir", str(self.data), "--config", str(self.config), "--port", "47844"]

    def profile(self, **data):
        (self.data / DESKTOP_PROFILE).write_text(json.dumps(data), encoding="utf8")

    def external_ui(self):
        directory = self.root / "source" / "packet_inspector" / "ui"
        directory.mkdir(parents=True)
        for name in RESOURCES:
            (directory / name).write_text("editable fixture", encoding="utf8")
        return directory

    def test_portable_developer_build_uses_editable_assets(self):
        embedded = self.root / "profile.json"
        embedded.write_text(json.dumps({"mode":"dev", "ui_dir":PORTABLE_UI}), encoding="utf8")
        profile = resolve_profile(load_profile(embedded), self.data)
        self.assertTrue(profile.development)
        self.assertTrue(profile.ui_dir.is_relative_to(self.data))
        self.assertEqual(profile.ui_dir.name, VERSION)
        self.assertTrue(all((profile.ui_dir / name).is_file() for name in RESOURCES))

    def test_release_download_retains_existing_developer_workspace(self):
        directory = self.external_ui()
        self.profile(mode="dev", ui_dir=str(directory), managed_ui=False)
        resolved = resolve_profile(BuildProfile(), self.data)
        self.assertTrue(resolved.development)
        self.assertEqual(resolved.ui_dir, directory)

    def test_developer_download_retains_installed_release_mode(self):
        self.profile(mode="release")
        self.assertEqual(resolve_profile(BuildProfile("dev"), self.data), BuildProfile())

    def test_managed_developer_assets_advance_to_new_version(self):
        self.profile(mode="dev", ui_dir=str(self.data / "development/ui/old"), managed_ui=True)
        self.assertEqual(resolve_profile(BuildProfile(), self.data).ui_dir.name, VERSION)

    def test_legacy_developer_window_remembers_workspace(self):
        directory = self.external_ui()
        old_exe = directory.parents[1] / ".local/dev-builds/old/CodexPacketInspector-Dev.exe"
        remember_upgrade_profile(self.data, BuildProfile(), [{"mode":"dev", "executable":str(old_exe)}])
        profile = installed_profile(self.data)
        self.assertEqual(profile["mode"], "dev")
        self.assertEqual(Path(profile["ui_dir"]), directory)

    def test_existing_release_window_wins_over_incoming_developer_default(self):
        remember_upgrade_profile(self.data, BuildProfile("dev"), [{"mode":"release"}])
        self.assertEqual(installed_profile(self.data)["mode"], "release")

    def test_corrupt_mode_is_not_silently_converted(self):
        self.profile(mode="unknown")
        with self.assertRaises(ValueError):
            resolve_profile(BuildProfile(), self.data)

    def test_application_is_its_own_verified_temporary_payload(self):
        observed = {}
        def install(args, **kwargs):
            payload = Path(args[args.index("--payload-dir") + 1])
            observed.update(kwargs)
            manifest = json.loads((payload / "manifest.json").read_text())
            self.assertEqual(manifest["version"], VERSION)
            self.assertEqual(manifest["sha256"]["CodexPacketInspector.exe"], hashlib.sha256(self.executable.read_bytes()).hexdigest())
            self.assertEqual((payload / "CodexPacketInspector.exe").read_bytes(), self.executable.read_bytes())
            self.assertIn("--quiet", args)
            return 0
        with patch("packet_inspector.upgrade.sys.executable", str(self.executable)), \
                patch("packet_inspector.upgrade.local_json", side_effect=ConnectionRefusedError), \
                patch("packet_inspector.upgrade.main", side_effect=install):
            self.assertEqual(run_integrated(self.arguments, quiet=True), 0)
        self.assertFalse(observed["open_after"])
        self.assertEqual(list((self.data / "updates/staging").iterdir()), [])

    def test_failed_install_cleans_staging(self):
        with patch("packet_inspector.upgrade.sys.executable", str(self.executable)), \
                patch("packet_inspector.upgrade.local_json", side_effect=ConnectionRefusedError), \
                patch("packet_inspector.upgrade.main", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                run_integrated(self.arguments, quiet=True)
        self.assertEqual(list((self.data / "updates/staging").iterdir()), [])

    def test_downloaded_foreground_exe_uses_integrated_upgrade(self):
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(self.executable)), \
                patch("packet_inspector.upgrade.run_integrated", return_value=0) as install, \
                patch("packet_inspector.desktop.run") as desktop:
            self.assertEqual(launch.main(self.arguments), 0)
        install.assert_called_once_with(self.arguments, quiet=False, force=False)
        desktop.assert_not_called()

    def test_legacy_settings_command_uses_integrated_upgrade(self):
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(self.executable)), \
                patch("packet_inspector.upgrade.run_integrated", return_value=0) as install:
            self.assertEqual(launch.main(["--quiet", *self.arguments]), 0)
        install.assert_called_once_with(self.arguments, quiet=True, force=True)

    def test_installed_worker_does_not_upgrade_recursively(self):
        executable = self.data / "bin/versions/fixture/CodexPacketInspector.exe"
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(executable)), \
                patch("packet_inspector.upgrade.run_integrated") as install, \
                patch("packet_inspector.desktop.run", return_value=0) as desktop:
            self.assertEqual(launch.main(self.arguments), 0)
        install.assert_not_called()
        desktop.assert_called_once()

    def test_explicit_upgrade_uses_same_main_program(self):
        with patch("packet_inspector.upgrade.run_integrated", return_value=0) as install:
            self.assertEqual(launch.main(["--upgrade", "--quiet", *self.arguments]), 0)
        install.assert_called_once_with(self.arguments, quiet=True, force=True)


if __name__ == "__main__":
    unittest.main()
