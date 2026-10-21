import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

import test_packet_inspector as fixtures
from test_capacity_retry import CREATED, FAILED, SUCCESS, Response
from packet_inspector.startup import configure_startup, startup_supported
from packet_inspector.server import InspectorServer


class RuntimeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    api = fixtures.RelayTests.api
    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest

    def setUp(self):
        self.server.save_settings({**self.server.settings, "start_on_login": False, "capacity_retry_enabled": False})
        self.server.startup_state_known = True

    def test_defaults_and_capabilities(self):
        status, result = self.api("/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(result["start_on_login"])
        self.assertFalse(result["capacity_retry_enabled"])
        self.assertFalse(result["start_on_login_supported"])
        self.assertTrue(result["capacity_retry_supported"])

    def test_capacity_setting_is_persisted_and_can_be_disabled(self):
        for enabled in [True, False]:
            status, result = self.api("/api/settings", {"capacity_retry_enabled": enabled})
            self.assertEqual(status, 200)
            self.assertEqual(result["capacity_retry_enabled"], enabled)
            saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
            self.assertEqual(saved["capacity_retry_enabled"], enabled)

    def test_boolean_validation_does_not_accept_numbers_or_strings(self):
        for key in ["start_on_login", "capacity_retry_enabled"]:
            for value in [1, "true", None]:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.api("/api/settings", {key: value})[0], 400)
                    self.assertFalse(self.server.settings[key])

    def test_login_setting_uses_installation_helper_without_stopping_service(self):
        with patch("packet_inspector.server.configure_startup") as configure:
            for enabled in [True, False]:
                status, result = self.api("/api/settings", {"start_on_login": enabled})
                self.assertEqual(status, 200)
                self.assertEqual(result["start_on_login"], enabled)
                configure.assert_called_with(enabled, self.server.data_dir, self.server.link.config, self.server.port)
                self.assertEqual(self.api("/health")[0], 200)
        self.assertEqual(configure.call_count, 2)

    def test_failed_startup_change_rolls_back_combined_settings(self):
        before = self.server.settings_path.read_bytes()
        with patch("packet_inspector.server.configure_startup", side_effect=ValueError("Task registration failed")):
            status, _ = self.api("/api/settings", {"start_on_login": True, "capacity_retry_enabled": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.server.settings_path.read_bytes(), before)
        self.assertFalse(self.server.settings["start_on_login"])
        self.assertFalse(self.server.settings["capacity_retry_enabled"])

    def test_unknown_startup_state_can_be_reconciled_without_changing_checkbox(self):
        self.server.startup_state_known = False
        with patch("packet_inspector.server.configure_startup") as configure:
            status, result = self.api("/api/settings", {"start_on_login": False})
        self.assertEqual(status, 200)
        configure.assert_called_once()
        self.assertTrue(result["startup_state_known"])

    def test_disposable_instance_cannot_modify_login_task(self):
        self.assertEqual(self.api("/api/settings", {"start_on_login": True})[0], 400)
        self.assertFalse(self.server.settings["start_on_login"])

    def test_enabled_retry_replays_same_request_and_records_retry(self):
        self.api("/api/settings", {"capacity_retry_enabled": True})
        first = Response(CREATED + FAILED, extra={"Retry-After": "0"})
        second = Response(SUCCESS)
        raw = b'{"model":"original","stream":true,"input":[]}'
        with patch.object(self.server.local_opener, "open", side_effect=[first, second]) as opened:
            status, data = self.relay(raw)
        self.assertEqual((status, data), (200, SUCCESS))
        self.assertEqual(opened.call_count, 2)
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertIs(requests[0], requests[1])
        self.assertEqual(requests[0].data, raw)
        self.assertEqual(requests[0].get_header("Accept-encoding"), "identity")
        metadata, _ = self.latest()
        self.assertEqual(metadata["capacity_retry_count"], 1)
        self.assertEqual(metadata["state"], "complete")

    def test_disabled_retry_passes_failure_unchanged(self):
        with patch.object(self.server.local_opener, "open", return_value=Response(CREATED + FAILED)) as opened:
            status, data = self.relay(b"{}")
        self.assertEqual((status, data, opened.call_count), (200, CREATED + FAILED, 1))


class StartupHelperTests(unittest.TestCase):
    def test_enable_and_disable_use_specific_task_actions(self):
        folder = Path("disposable")
        with patch("packet_inspector.startup.startup_supported", return_value=True), \
                patch("packet_inspector.startup.manage_installation", return_value={"supported": True}) as manage:
            configure_startup(True, folder, folder / "config.toml", 1234)
            self.assertEqual(manage.call_args.args[0], "EnableStartup")
            configure_startup(False, folder, folder / "config.toml", 1234)
            self.assertEqual(manage.call_args.args[0], "DisableStartup")

    def test_source_or_disposable_directories_cannot_configure_startup(self):
        self.assertFalse(startup_supported(Path("disposable")))
        with self.assertRaisesRegex(ValueError, "Windows"):
            configure_startup(True, Path("disposable"), Path("config.toml"), 1234)


class StartupInitializationTests(unittest.TestCase):
    def test_existing_login_state_is_read_without_changing_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            with patch("packet_inspector.server.startup_supported", return_value=True), \
                    patch("packet_inspector.server.manage_installation", return_value={"login_start": True}) as manage:
                server = InspectorServer(("127.0.0.1", 0), root / "data", config, "http://127.0.0.1:9/v1")
                try:
                    self.assertTrue(server.settings["start_on_login"])
                    self.assertTrue(server.startup_state_known)
                    self.assertEqual(manage.call_args.args[0], "Status")
                finally:
                    server.server_close()

    def test_unavailable_task_scheduler_does_not_prevent_service_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            with patch("packet_inspector.server.startup_supported", return_value=True), \
                    patch("packet_inspector.server.manage_installation", side_effect=ValueError("Unavailable")):
                server = InspectorServer(("127.0.0.1", 0), root / "data", config, "http://127.0.0.1:9/v1")
                try:
                    self.assertFalse(server.startup_state_known)
                    self.assertFalse(server.settings["start_on_login"])
                    self.assertEqual(server.status()["storage"]["count"], 0)
                finally:
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
