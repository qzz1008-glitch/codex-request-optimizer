"""Fresh defaults must not replace saved choices during restart or cutover."""
import json
from pathlib import Path
import tempfile
import unittest

from packet_inspector.server import InspectorServer


class InstallationSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="fixture"\nmodel_context_window=280000\n'
                               'model_auto_compact_token_limit=250000\n[tools]\nview_image=true\n', encoding="utf8")
        self.data = self.root / "data"

    def server(self, **options):
        server = InspectorServer(("127.0.0.1", 0), self.data, self.config,
                                 "https://example.invalid/v1", **options)
        self.addCleanup(server.server_close)
        return server

    def test_first_install_enables_only_zstd_optional_feature(self):
        original = self.config.read_bytes()
        server = self.server()
        status = server.status()
        self.assertEqual(status["connection_state"], "disconnected")
        self.assertNotIn("request_compression", server.settings)
        self.assertEqual(status["max_detail_packets"], status["max_packets"])
        for key in ("packet_size_protection_enabled", "image_isolation_enabled",
                    "capacity_retry_enabled", "start_on_login"):
            self.assertIs(status[key], False, key)
        self.assertIs(server.updates.settings()["automatic"], False)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(status["profile"]["active"])
        self.assertEqual(server.auto_sync_providers(), {"enabled": False})
        self.assertEqual(self.config.read_bytes(), original)

    def test_legacy_retention_defaults_details_to_the_saved_record_limit(self):
        self.data.mkdir()
        (self.data / "settings.json").write_text(json.dumps({"max_packets": 1234, "max_storage_mb": 97,
                                                            "auto_detect_providers": False}), encoding="utf8")
        server = self.server()
        self.assertEqual(server.settings["max_packets"], 1234)
        self.assertEqual(server.settings["max_storage_mb"], 97)
        self.assertEqual(server.settings["max_detail_packets"], 1234)
        saved = json.loads(server.settings_path.read_text(encoding="utf8"))
        self.assertEqual(saved["max_detail_packets"], 1234)

    def test_restart_and_worker_activation_preserve_all_saved_choices(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.data = self.root / str(enabled)
                self.data.mkdir()
                values = {"relay_token": "test-preserved-token", "request_compression": "off" if enabled else "zstd",
                          "packet_size_protection_enabled": enabled, "image_isolation_enabled": enabled,
                          "capacity_retry_enabled": enabled, "capacity_retry_max_retries": 7,
                          "start_on_login": enabled, "auto_detect_providers": False,
                          "budget_bytes": 777000, "max_storage_mb": 97, "max_packets": 1234, "max_detail_packets": 321,
                          "image_save_dir": str(self.root / "custom-images"), "ui_language": "en",
                          "custom_future_setting": {"keep": True}}
                (self.data / "settings.json").write_text(json.dumps(values), encoding="utf8")
                original = self.config.read_bytes()
                active = self.server()
                for key, value in values.items():
                    if key != "request_compression":
                        self.assertEqual(active.settings[key], value, key)
                candidate = self.server(worker_id="a" * 32)
                # A user edit after the candidate started must also survive cutover.
                latest = {**values, "budget_bytes": 888000, "capacity_retry_max_retries": 9}
                active.save_settings({**active.settings, **latest})
                candidate.worker_control({"action": "activate", "public_port": 48765})
                for key, value in latest.items():
                    if key != "request_compression":
                        self.assertEqual(candidate.settings[key], value, key)
                saved = json.loads(candidate.settings_path.read_text(encoding="utf8"))
                for key, value in latest.items():
                    if key != "request_compression":
                        self.assertEqual(saved[key], value, key)
                self.assertEqual(self.config.read_bytes(), original)

    def test_inactive_worker_activation_does_not_implicitly_connect(self):
        original = self.config.read_bytes()
        candidate = self.server(worker_id="b" * 32)
        candidate.worker_control({"action": "activate", "public_port": 48765})
        self.assertFalse(candidate.status()["profile"]["active"])
        self.assertEqual(self.config.read_bytes(), original)

    def test_configuration_already_connected_requires_no_write_or_restart(self):
        server = self.server()
        self.assertTrue(server.sync_providers()["reload_required"])
        before = self.config.read_bytes()
        result = server.sync_providers()
        self.assertFalse(result["reload_required"])
        self.assertFalse(result["changed"])
        self.assertTrue(server.status()["profile"]["active"])
        self.assertTrue(server.status()["profile"]["endpoint_matches"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_configuration_for_another_entry_is_not_treated_as_current(self):
        server = self.server()
        server.sync_providers()
        before = self.config.read_bytes()
        server.public_port = 48765 if server.public_port != 48765 else 48766
        self.assertTrue(server.link.profile()["active"])
        self.assertFalse(server.status()["profile"]["active"])
        self.assertFalse(server.status()["profile"]["endpoint_matches"])
        server.auto_sync_providers()
        self.assertEqual(self.config.read_bytes(), before)

    def test_actual_configuration_wins_over_missing_recovery_metadata(self):
        server = self.server()
        server.sync_providers()
        before = self.config.read_bytes()
        server.link.state_path.unlink()
        self.assertFalse(server.link.profile()["active"])
        self.assertTrue(server.status()["profile"]["active"])
        result = server.auto_sync_providers()
        self.assertFalse(result["reload_required"])
        self.assertTrue(server.status()["profile"]["can_restore"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_connected_worker_upgrade_keeps_configuration_and_enabled_choices(self):
        active = self.server()
        active.sync_providers()
        choices = {"request_compression": "off", "packet_size_protection_enabled": True,
                   "image_isolation_enabled": True, "capacity_retry_enabled": True,
                   "start_on_login": True, "budget_bytes": 822000, "ui_language": "en"}
        active.save_settings({**active.settings, **choices})
        active.sync_providers()  # Apply native retry configuration before capturing an active installation.
        before = self.config.read_bytes()
        candidate = self.server(worker_id="c" * 32, public_port=active.public_port)
        candidate.worker_control({"action": "activate", "public_port": active.public_port})
        self.assertTrue(candidate.status()["profile"]["active"])
        for key, value in choices.items():
            if key != "request_compression":
                self.assertEqual(candidate.settings[key], value, key)
        self.assertNotIn("request_compression", candidate.settings)
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
