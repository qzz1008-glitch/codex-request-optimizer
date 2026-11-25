"""Main EXEs install themselves and preserve the installed desktop mode."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector import VERSION, launch
from packet_inspector.frontend_dev import (BuildProfile, DESKTOP_PROFILE, PORTABLE_UI, RESOURCES,
    installed_profile, load_profile, remember_upgrade_profile, resolve_profile)
from packet_inspector.managed_runtime import sha256_file
from packet_inspector.router import PROTOCOL, atomic_json
from packet_inspector.upgrade import run_integrated
from packet_inspector.updates import ASSET


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

    def run_launcher(self, source, version, *, from_settings=False, handover_error=None):
        def commit(engine):
            if handover_error:
                raise handover_error
            package = engine.payload / ASSET
            worker = self.data / "bin" / "versions" / sha256_file(package) / ASSET
            worker.parent.mkdir(parents=True, exist_ok=True)
            worker.write_bytes(package.read_bytes())
            (self.data / "bin" / ASSET).write_bytes(b"stable entry")
            identity = "a" * 32
            atomic_json(self.data / "router-state.json", {"active": identity, "protocol": PROTOCOL, "config": str(self.config)})
            atomic_json(self.data / "router-workers" / (identity + ".json"),
                        {"id": identity, "port": 49123, "executable": str(worker), "version": version})
            return {"state": "complete", "version": version}

        with patch("packet_inspector.VERSION", version), \
                patch("packet_inspector.upgrade.sys.executable", str(source)), \
                patch("packet_inspector.upgrade.local_json", side_effect=ConnectionRefusedError), \
                patch("packet_inspector.upgrade.Upgrade._run", autospec=True, side_effect=commit), \
                patch("packet_inspector.upgrade_cleanup.cleanup_versions", return_value={}), \
                redirect_stdout(io.StringIO()):
            return run_integrated(self.arguments, quiet=True, force=from_settings)

    def settings_download(self, version, contents):
        source = self.data / "updates" / version / ASSET
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(contents)
        atomic_json(self.data / "update-state.json", {"state": "upgrading", "latest_version": version,
                                                     "asset": {"sha256": sha256_file(source)}})
        return source

    def test_successive_settings_upgrades_replace_the_same_downloaded_exe(self):
        self.run_launcher(self.executable, "9.0.0")
        record = self.data / "downloaded-launcher.json"
        for version in ("9.0.1", "9.0.2"):
            with self.subTest(version=version):
                source = self.settings_download(version, version.encode())
                self.run_launcher(source, version, from_settings=True)
                self.assertEqual(self.executable.read_bytes(), version.encode())
                original = json.loads(record.read_text(encoding="utf8"))
                self.assertEqual(original["path"], str(self.executable))
                self.assertEqual(original["sha256"], sha256_file(source))
        self.assertEqual(list(self.root.glob(".optimizer-update-*.tmp")), [])

    def test_settings_upgrade_can_recover_an_existing_installations_original_exe(self):
        self.run_launcher(self.executable, "9.0.0")
        (self.data / "downloaded-launcher.json").unlink()
        source = self.settings_download("9.0.1", b"new GitHub executable")
        self.run_launcher(source, "9.0.1", from_settings=True)
        self.assertEqual(self.executable.read_bytes(), source.read_bytes())

    def test_settings_upgrade_leaves_an_exe_whose_contents_changed(self):
        self.run_launcher(self.executable, "9.0.0")
        self.executable.write_bytes(b"different executable using the old name")
        source = self.settings_download("9.0.1", b"new GitHub executable")
        self.run_launcher(source, "9.0.1", from_settings=True)
        self.assertEqual(self.executable.read_bytes(), b"different executable using the old name")
        result = json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8"))
        self.assertEqual((result["state"], result["downloaded_launcher"]), ("complete", "changed"))

    def test_settings_upgrade_leaves_the_download_if_handover_fails(self):
        original = self.executable.read_bytes()
        self.run_launcher(self.executable, "9.0.0")
        source = self.settings_download("9.0.1", b"new GitHub executable")
        self.assertEqual(self.run_launcher(source, "9.0.1", from_settings=True,
                                          handover_error=ValueError("handover failed")), 1)
        self.assertEqual(self.executable.read_bytes(), original)

    def test_opening_the_same_download_cold_reuses_the_installed_runtime(self):
        contents = self.executable.read_bytes()
        version = self.data / "bin" / "versions" / hashlib.sha256(contents).hexdigest()
        runtime = version / "runtime" / self.executable.name
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"installed runtime")
        (version / self.executable.name).write_bytes(contents)
        (self.data / "bin" / self.executable.name).write_bytes(contents)
        identity = "a" * 32
        atomic_json(self.data / "router-state.json", {"active": identity, "protocol": PROTOCOL, "config": str(self.config)})
        atomic_json(self.data / "router-workers" / (identity + ".json"),
                    {"id": identity, "port": 49123, "token": "test", "executable": str(runtime)})
        with patch("packet_inspector.upgrade.sys.executable", str(self.executable)), \
                patch("packet_inspector.upgrade.local_json", side_effect=ConnectionRefusedError) as probe, \
                patch("packet_inspector.upgrade.subprocess.Popen") as launch_installed, \
                patch("packet_inspector.upgrade.main") as install:
            self.assertEqual(run_integrated(self.arguments), 0)
        self.assertEqual(launch_installed.call_args.args[0][:2], [str(runtime), "--router"])
        probe.assert_not_called()
        install.assert_not_called()
        recorded = json.loads((self.data / "downloaded-launcher.json").read_text(encoding="utf8"))
        self.assertEqual(recorded["path"], str(self.executable))

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
