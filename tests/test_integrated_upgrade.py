"""Downloaded EXEs must distinguish same-version builds without touching live services."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector import upgrade
from packet_inspector.frontend_dev import BuildProfile
from packet_inspector.router import atomic_json


class IntegratedUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "config.toml"
        self.config.write_text('model = "fixture"\n', encoding="utf8")
        self.source = self.root / "download.exe"
        self.source.write_bytes(b"new application")
        self.canonical = self.data / "bin" / "CodexPacketInspector.exe"
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_bytes(b"stable entry from an earlier installation")
        self.status = {"version": "9.0.0", "data_dir": str(self.data),
                       "profile": {"config_path": str(self.config)}, "worker_id": "a" * 32}
        self.active = self.install_worker(b"old application")
        self.arguments = ["--data-dir", str(self.data), "--config", str(self.config), "--port", "47843"]
        self.staged = []
        self.result = 0
        for target, values in (
            ("packet_inspector.VERSION", {"new": "9.0.0"}),
            ("packet_inspector.upgrade.sys.executable", {"new": str(self.source)}),
            ("packet_inspector.upgrade.local_json", {"side_effect": self.api}),
            ("packet_inspector.upgrade.load_profile", {"return_value": BuildProfile()}),
        ):
            patcher = patch(target, **values)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(upgrade, "main", side_effect=self.stage)
        self.installer = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(upgrade.Upgrade, "_open_window")
        self.opener = patcher.start()
        self.addCleanup(patcher.stop)

    def install_worker(self, content):
        executable = self.data / "bin" / "versions" / hashlib.sha256(content).hexdigest() / "CodexPacketInspector.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(content)
        atomic_json(self.data / "router-workers" / (self.status["worker_id"] + ".json"),
                    {"id": self.status["worker_id"], "port": 49123, "token": "fixture-control",
                     "version": self.status["version"], "executable": str(executable)})
        return executable

    def api(self, _port, path, **_kwargs):
        return {"app": "codex-packet-inspector"} if path == "/health" else self.status

    def stage(self, arguments, **keywords):
        payload = Path(arguments[arguments.index("--payload-dir") + 1])
        manifest = json.loads((payload / "manifest.json").read_text(encoding="utf8"))
        self.staged.append({"path": payload, "manifest": manifest, "arguments": arguments, "keywords": keywords,
                            "content": (payload / "CodexPacketInspector.exe").read_bytes()})
        return self.result

    def report(self):
        return json.loads((self.data / "launch-status.json").read_text(encoding="utf8"))

    def test_same_version_changed_build_enters_verified_installer(self):
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.opener.assert_not_called()
        self.installer.assert_called_once()
        staged = self.staged[0]
        self.assertEqual(staged["content"], self.source.read_bytes())
        self.assertEqual(staged["manifest"]["sha256"]["CodexPacketInspector.exe"], upgrade.digest(self.source))
        self.assertTrue(staged["keywords"]["open_after"])
        self.assertFalse(staged["path"].exists())
        self.assertEqual(self.report()["reason"], "same_version_payload_changed")
        self.assertEqual(self.report()["state"], "complete")
        self.assertEqual(self.report()["installed_sha256"], upgrade.digest(self.active))
        self.assertEqual(self.config.read_text(encoding="utf8"), 'model = "fixture"\n')

    def test_identical_running_build_opens_without_reinstalling_older_stable_entry(self):
        self.install_worker(self.source.read_bytes())
        self.assertNotEqual(upgrade.digest(self.canonical), upgrade.digest(self.source))
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.opener.assert_called_once()
        self.installer.assert_not_called()
        self.assertEqual(self.report()["reason"], "same_payload")
        self.assertEqual(self.report()["action"], "open_installed")

    def test_matching_stable_entry_cannot_hide_a_different_running_build(self):
        self.canonical.write_bytes(self.source.read_bytes())
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.installer.assert_called_once()
        self.opener.assert_not_called()

    def test_legacy_service_without_build_identity_enters_normal_upgrade(self):
        self.status.pop("worker_id")
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.installer.assert_called_once()
        self.assertIsNone(self.report()["installed_sha256"])

    def test_newer_public_version_is_opened_and_forced_downgrade_is_refused(self):
        self.status["version"] = "9.0.1"
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.opener.assert_called_once()
        self.installer.assert_not_called()
        self.assertEqual(self.report()["reason"], "newer_installed")
        with self.assertRaisesRegex(ValueError, "未执行降级"):
            upgrade.run_integrated(self.arguments, force=True)
        self.installer.assert_not_called()

    def test_newer_public_package_uses_normal_upgrade(self):
        self.status["version"] = "8.9.0"
        self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.installer.assert_called_once()
        self.assertEqual(self.report()["reason"], "newer_payload")

    def test_explicit_quiet_upgrade_can_reinstall_identical_build(self):
        self.install_worker(self.source.read_bytes())
        self.assertEqual(upgrade.run_integrated(self.arguments, quiet=True, force=True), 0)
        self.installer.assert_called_once()
        self.opener.assert_not_called()
        self.assertIn("--quiet", self.staged[0]["arguments"])
        self.assertFalse(self.staged[0]["keywords"]["open_after"])
        self.assertEqual(self.report()["reason"], "explicit_upgrade")

    def test_service_not_running_still_installs(self):
        with patch.object(upgrade, "local_json", side_effect=ConnectionRefusedError):
            self.assertEqual(upgrade.run_integrated(self.arguments), 0)
        self.installer.assert_called_once()
        self.assertEqual(self.report()["reason"], "service_not_running")

    def test_foreign_service_and_unmanaged_worker_are_never_overwritten(self):
        for foreign in (True, False):
            with self.subTest(foreign=foreign):
                if foreign:
                    self.status["data_dir"] = str(self.root / "other")
                else:
                    self.status["data_dir"] = str(self.data)
                    metadata = self.data / "router-workers" / (self.status["worker_id"] + ".json")
                    worker = json.loads(metadata.read_text(encoding="utf8"))
                    worker["executable"] = str(self.source)
                    atomic_json(metadata, worker)
                with self.assertRaises(ValueError):
                    upgrade.run_integrated(self.arguments)
                self.installer.assert_not_called()
                self.opener.assert_not_called()

    def test_latest_launch_report_is_overwritten_and_failure_is_recorded(self):
        atomic_json(self.data / "launch-status.json", {"previous": "x" * 10000})
        self.result = 1
        self.assertEqual(upgrade.run_integrated(self.arguments), 1)
        report = self.report()
        self.assertNotIn("previous", report)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["exit_code"], 1)
        self.assertLess((self.data / "launch-status.json").stat().st_size, 2500)
        self.assertNotIn("fixture-control", json.dumps(report))
        self.assertEqual(list(self.data.glob("launch-status.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
