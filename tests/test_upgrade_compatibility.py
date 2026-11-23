"""Upgrade configuration changes are explicit, reversible and mode preserving."""
import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig
from packet_inspector.restart_notice import RestartNotices
from packet_inspector.upgrade_compatibility import configuration_upgrade


class ConfigurationUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n[tools]\nweb_search=false\n', encoding="utf8")
        self.settings = self.data / "settings.json"
        self.state = self.data / "image-tool.json"
        probe = patch("packet_inspector.image_config.verify_view_image_feature")
        self.probe = probe.start()
        self.addCleanup(probe.stop)
        stamp = patch.object(ImageToolConfig, "native_configuration_stamp", return_value={"build": "new"})
        self.stamp = stamp.start()
        self.addCleanup(stamp.stop)

    def disabled(self):
        manager = ImageToolConfig(ConfigLink(self.config, self.data / "codex-link.json"), self.data, lambda: "")
        with manager.change("disabled"):
            pass
        self.config.write_text(self.config.read_text(encoding="utf8").replace("[tools]", "[tools]\nview_image=false"), encoding="utf8")
        state = json.loads(self.state.read_text(encoding="utf8"))
        state["native_capability"] = {"build": "old"}
        self.state.write_text(json.dumps(state), encoding="utf8")
        self.settings.write_text(json.dumps({"image_analysis_mode": "disabled", "relay_token": "preserve"}), encoding="utf8")
        self.probe.reset_mock()

    def older_image_tool(self):
        cli = self.root / "codex"
        cli.touch()
        manager = ImageToolConfig(ConfigLink(self.config, self.data / "codex-link.json"), self.data, lambda: "")
        with patch("packet_inspector.image_config.find_codex_cli", return_value=str(cli)), \
             patch.object(manager, "_validate"), manager.change("optimized"):
            pass
        script = manager.installation.directory / "server.ps1"
        old = b"# Earlier managed image tool\n"
        script.write_bytes(old)
        manifest = manager.installation.manifest
        state = json.loads(manifest.read_text(encoding="utf8"))
        state["files"][script.name] = hashlib.sha256(old).hexdigest()
        manifest.write_text(json.dumps(state), encoding="utf8")
        self.settings.write_text(json.dumps({"image_analysis_mode": "optimized"}), encoding="utf8")
        return script

    def test_default_native_mode_does_not_create_optional_configuration(self):
        before = self.config.read_bytes()
        with configuration_upgrade(self.data, self.config) as result:
            self.assertEqual(result["image_tools"]["state"], "skipped")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.state.exists())
        self.probe.assert_not_called()

    def test_legacy_disabled_mode_is_cleaned_once_and_remains_disabled(self):
        self.disabled()
        settings = self.settings.read_bytes()
        for _ in range(2):
            with configuration_upgrade(self.data, self.config):
                parsed = tomllib.loads(self.config.read_text(encoding="utf8"))
                self.assertEqual(parsed["tools"], {"web_search": False})
                self.assertIs(parsed["features"]["view_image"], False)
        self.probe.assert_called_once()
        self.assertEqual(self.settings.read_bytes(), settings)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf8"))["mode"], "disabled")

    def test_failed_handover_restores_migration_files_and_completion_stamp(self):
        self.disabled()
        before = {path: path.read_bytes() for path in (self.config, self.settings, self.state)}
        with self.assertRaisesRegex(RuntimeError, "handover failed"):
            with configuration_upgrade(self.data, self.config):
                raise RuntimeError("handover failed")
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw, path.name)
        self.assertFalse(RestartNotices(self.data, self.config).snapshot()["items"])

    def test_reload_notice_is_added_only_after_the_configuration_commit(self):
        self.disabled()
        notices = RestartNotices(self.data, self.config)
        with configuration_upgrade(self.data, self.config):
            self.assertFalse(notices.snapshot()["items"])
        self.assertTrue(any(item["key"] == "image_analysis_mode" for item in notices.snapshot()["items"]))

    def test_asset_only_upgrade_preserves_config_and_marks_restart_once(self):
        with patch("packet_inspector.image_tool_install.find_powershell", return_value="powershell.exe"), \
             patch.object(ImageToolConfig, "available_modes", return_value=("optimized",)):
            script = self.older_image_tool()
            before, old = self.config.read_bytes(), script.read_bytes()
            notices = RestartNotices(self.data, self.config)
            with configuration_upgrade(self.data, self.config) as result:
                self.assertTrue(result["image_tools"]["changed"])
                self.assertNotEqual(script.read_bytes(), old)
                self.assertFalse(notices.snapshot()["items"])
            self.assertEqual(self.config.read_bytes(), before)
            marked = notices.snapshot()
            self.assertTrue(any(item["key"] == "image_analysis_mode" for item in marked["items"]))
            with configuration_upgrade(self.data, self.config) as result:
                self.assertFalse(result["image_tools"]["changed"])
            self.assertEqual(notices.snapshot()["revision"], marked["revision"])

    def test_asset_only_upgrade_rollback_preserves_concurrent_config_edit(self):
        with patch("packet_inspector.image_tool_install.find_powershell", return_value="powershell.exe"), \
             patch.object(ImageToolConfig, "available_modes", return_value=("optimized",)):
            script = self.older_image_tool()
            manifest = script.with_name("installation.json")
            old = script.read_bytes(), manifest.read_bytes()
            edited = b'user_value="preserve"\n' + self.config.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "handover failed"):
                with configuration_upgrade(self.data, self.config) as result:
                    self.assertTrue(result["image_tools"]["changed"])
                    self.config.write_bytes(edited)
                    raise RuntimeError("handover failed")
            self.assertEqual((script.read_bytes(), manifest.read_bytes()), old)
            self.assertEqual(self.config.read_bytes(), edited)
            self.assertFalse(RestartNotices(self.data, self.config).snapshot()["items"])

    def test_external_configuration_edits_are_preserved_on_rollback(self):
        self.disabled()
        with self.assertRaisesRegex(ValueError, "并发修改"):
            with configuration_upgrade(self.data, self.config):
                self.config.write_text(self.config.read_text(encoding="utf8") + '\nuser_value="preserve"\n', encoding="utf8")
                raise RuntimeError("handover failed")
        self.assertIn('user_value="preserve"', self.config.read_text(encoding="utf8"))


if __name__ == "__main__":
    unittest.main()
