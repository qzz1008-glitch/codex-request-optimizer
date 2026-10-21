"""Local developer opt-in must never change the relay or release defaults."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dev_inspector_ui
import test_desktop_ui as desktop_tests
from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.frontend_dev import CONFIG_NAME, FrontendDevelopment, RESOURCES, register


class FrontendDevelopmentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "source"
        self.source.mkdir()
        for name in RESOURCES:
            (self.source / name).write_text(name, encoding="utf8")

    def test_absent_local_registration_stays_off_without_creating_files(self):
        config = self.folder / CONFIG_NAME
        mode = FrontendDevelopment(config)
        self.assertFalse(mode.available)
        self.assertFalse(mode.enabled)
        self.assertFalse(config.exists())
        with self.assertRaises(ValueError):
            mode.set_enabled(True)

    def test_invalid_registration_fails_closed(self):
        config = self.folder / CONFIG_NAME
        for text in ['{"enabled":"true"}', '[]', '{broken']:
            with self.subTest(text=text):
                config.write_text(text, encoding="utf8")
                mode = FrontendDevelopment(config)
                self.assertFalse(mode.enabled)
                self.assertTrue(mode.error)

    def test_failed_save_preserves_the_last_mode_and_cleans_temporary_file(self):
        config = register(self.folder, self.source, False)
        mode = FrontendDevelopment(config)
        before = config.read_bytes()
        with patch("packet_inspector.frontend_dev.Path.replace", side_effect=OSError("test disk failure")):
            with self.assertRaises(OSError):
                mode.set_enabled(True)
        self.assertFalse(mode.enabled)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_cli_registration_does_not_start_or_contact_any_service(self):
        with patch.object(dev_inspector_ui, "check_relay") as check, \
                patch.object(dev_inspector_ui, "DesktopUI") as service, redirect_stdout(io.StringIO()):
            self.assertEqual(dev_inspector_ui.main(["--desktop-mode", "on", "--data-dir", str(self.folder)]), 0)
            self.assertTrue(FrontendDevelopment(self.folder / CONFIG_NAME).enabled)
            self.assertEqual(dev_inspector_ui.main(["--desktop-mode", "off", "--data-dir", str(self.folder)]), 0)
            self.assertFalse(FrontendDevelopment(self.folder / CONFIG_NAME).enabled)
        check.assert_not_called()
        service.assert_not_called()


class FrontendDevelopmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = desktop_tests.DesktopUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.request = self.fixture.request
        self.source = self.fixture.folder / "source"
        self.source.mkdir()
        for name in RESOURCES:
            text = "<html><head></head><body>external frontend</body></html>" if name == "index.html" else "external frontend"
            (self.source / name).write_text(text, encoding="utf8")
        self.config = self.fixture.folder / CONFIG_NAME

    def configure(self, enabled=False):
        register(self.fixture.folder, self.source, enabled)
        self.fixture.ui.close()
        self.fixture.ui = DesktopUI(self.fixture.backend.server_port, ui_dir=self.fixture.folder,
                                    development_config=self.config).start()

    def set_mode(self, enabled):
        return self.request("/api/ui-development", "POST", json.dumps({"enabled": enabled}).encode())

    def test_release_has_no_developer_api_or_reload_script(self):
        self.assertEqual(self.request("/api/ui-development")[0], 404)
        self.assertEqual(self.request("/__dev__/reload.js")[0], 404)
        self.assertNotIn(b"/__dev__/reload.js", self.request("/")[1])
        status = json.loads(self.request("/api/status")[1])
        self.assertFalse(status["ui_development"]["available"])
        self.assertFalse(status["ui_live_reload"])
        self.assertFalse(self.config.exists())

    def test_toggle_swaps_resources_and_all_reload_features_together(self):
        self.configure()
        original = self.request("/")[1]
        self.assertEqual(self.set_mode(True)[0], 200)
        self.assertIn(b"external frontend", self.request("/")[1])
        self.assertIn(b"/__dev__/reload.js", self.request("/")[1])
        before = json.loads(self.request("/__dev__/state")[1])["build"]
        (self.source / "style.css").write_text("changed external frontend", encoding="utf8")
        self.assertNotEqual(before, json.loads(self.request("/__dev__/state")[1])["build"])
        self.assertIn(b"changed external", self.request("/style.css")[1])
        self.assertTrue(json.loads(self.request("/api/status")[1])["ui_live_reload"])
        self.assertEqual(self.set_mode(False)[0], 200)
        self.assertEqual(self.request("/")[1], original)
        self.assertEqual(self.request("/__dev__/reload.js")[0], 404)
        self.assertEqual(self.request("/__dev__/state")[0], 404)
        self.assertFalse(json.loads(self.request("/api/status")[1])["ui_live_reload"])
        self.assertFalse(FrontendDevelopment(self.config).enabled)
        self.assertEqual([r for r in self.fixture.backend.requests if r[0] == "POST"], [])

    def test_off_mode_ignores_missing_source_and_survives_reopening(self):
        self.configure(False)
        for name in RESOURCES:
            (self.source / name).unlink()
        self.fixture.ui.close()
        self.fixture.ui = DesktopUI(self.fixture.backend.server_port, ui_dir=self.fixture.folder,
                                    development_config=self.config).start()
        self.assertIn(b"new UI", self.request("/")[1])
        self.assertFalse(self.fixture.ui.live_reload)
        self.assertFalse(self.fixture.ui.development.error)

    def test_invalid_source_cannot_enable_or_modify_saved_mode(self):
        self.configure(False)
        before = self.config.read_bytes()
        (self.source / "app.js").unlink()
        self.assertEqual(self.set_mode(True)[0], 400)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.fixture.ui.live_reload)
        self.assertIn(b"new UI", self.request("/")[1])

    def test_only_boolean_mode_is_accepted_and_browser_cannot_replace_the_source_path(self):
        self.configure(False)
        for value in [None, 1, "true", []]:
            with self.subTest(value=value):
                self.assertEqual(self.set_mode(value)[0], 400)
        body = json.dumps({"enabled": True, "ui_dir": str(self.fixture.folder)}).encode()
        self.assertEqual(self.request("/api/ui-development", "POST", body)[0], 400)
        self.assertFalse(self.fixture.ui.live_reload)

    def test_read_only_preview_cannot_change_the_local_developer_switch(self):
        self.configure(False)
        self.fixture.ui.read_only = True
        self.assertEqual(self.set_mode(True)[0], 409)
        self.assertFalse(FrontendDevelopment(self.config).enabled)


if __name__ == "__main__":
    unittest.main()
