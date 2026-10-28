"""The developer EXE has fixed external assets; release has no developer switch."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_inspector
from tests import test_desktop_ui as desktop_tests
from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.frontend_dev import BuildProfile, RESOURCES, load_profile, resolve_profile, DESKTOP_PROFILE


class BuildProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.profile = self.folder / "profile.json"

    def write(self, value):
        self.profile.write_text(json.dumps(value), encoding="utf8")

    def test_source_and_explicit_release_use_bundled_assets(self):
        self.assertEqual(load_profile(self.profile), BuildProfile())
        self.write({"mode": "release"})
        self.assertEqual(load_profile(self.profile), BuildProfile())
        self.assertEqual(build_inspector.build_profile("release"), {"mode": "release"})

    def test_developer_profile_requires_all_external_resources(self):
        self.write({"mode": "dev", "ui_dir": str(self.folder)})
        with self.assertRaises(ValueError):
            load_profile(self.profile)
        for name in RESOURCES:
            (self.folder / name).write_text("test", encoding="utf8")
        mode = load_profile(self.profile)
        self.assertTrue(mode.development)
        self.assertEqual(mode.ui_dir, self.folder.resolve())

    def test_release_cannot_load_an_external_path_or_runtime_switch(self):
        for value in [
            {"mode": "release", "ui_dir": str(self.folder)},
            {"enabled": True, "ui_dir": str(self.folder)},
            {"mode": "dev", "ui_dir": "relative/path"},
            {"mode": "dev", "ui_dir": None},
            {"mode": "other"}, [],
        ]:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    load_profile(self.profile)

    def test_malformed_profile_fails_clearly(self):
        for text in ["{", " " * 8193]:
            self.profile.write_text(text, encoding="utf8")
            with self.assertRaises(ValueError):
                load_profile(self.profile)

    def test_build_outputs_are_separate_and_release_is_default(self):
        self.assertIn("release", build_inspector.output_directory().parts)
        self.assertIn("dev-builds", build_inspector.output_directory(mode="dev").parts)
        profile = build_inspector.build_profile("dev")
        self.assertEqual(profile["mode"], "dev")
        self.assertTrue(Path(profile["ui_dir"]).is_absolute())
        self.assertEqual(Path(profile["ui_dir"]).name, "ui")
        with self.assertRaises(ValueError):
            build_inspector.build_profile("invalid")

    def test_same_version_new_build_does_not_reuse_managed_old_frontend(self):
        data = self.folder / "installed"
        data.mkdir()
        (data / DESKTOP_PROFILE).write_text(json.dumps({"mode": "dev", "managed_ui": True}))
        with patch("packet_inspector.frontend_dev.build_info", return_value={"source_sha256": "a" * 64}):
            first = resolve_profile(BuildProfile("dev"), data)
            (first.ui_dir / "app.js").write_text("old managed UI")
            self.assertEqual(resolve_profile(BuildProfile("dev"), data).ui_dir, first.ui_dir)
            self.assertEqual((first.ui_dir / "app.js").read_text(), "old managed UI")
        with patch("packet_inspector.frontend_dev.build_info", return_value={"source_sha256": "b" * 64}):
            second = resolve_profile(BuildProfile("dev"), data)
            self.assertNotEqual(first.ui_dir, second.ui_dir)
            self.assertNotEqual((second.ui_dir / "app.js").read_text(encoding="utf8"), "old managed UI")

    def test_new_build_preserves_explicit_live_source_directory(self):
        for name in RESOURCES:
            (self.folder / name).write_text("editable source")
        data = self.folder / "installed"
        data.mkdir()
        (data / DESKTOP_PROFILE).write_text(json.dumps({"mode": "dev", "managed_ui": False,
                                                      "ui_dir": str(self.folder.resolve())}))
        with patch("packet_inspector.frontend_dev.build_info", return_value={"source_sha256": "b" * 64}):
            self.assertEqual(resolve_profile(BuildProfile("dev"), data).ui_dir, self.folder.resolve())


class FixedFrontendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = desktop_tests.DesktopUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.request = self.fixture.request

    def developer_ui(self):
        self.fixture.ui.close()
        folder = self.fixture.folder / "source"
        folder.mkdir()
        for name in RESOURCES:
            (folder / name).write_text((self.fixture.folder / name).read_text(encoding="utf8"),
                                       encoding="utf8")
        self.fixture.ui = DesktopUI(self.fixture.backend.server_port, ui_dir=folder, live_reload=True).start()
        return folder

    def test_release_has_no_developer_endpoints_or_injected_script(self):
        self.assertNotIn(b"/__dev__/", self.request("/")[1])
        for path in ["/__dev__/state", "/__dev__/reload.js", "/api/ui-development"]:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/ui-development", "POST", b'{"enabled":true}')[0], 404)
        status = json.loads(self.request("/api/status")[1])
        self.assertFalse(status["ui_live_reload"])
        self.assertNotIn("ui_development", status)
        self.assertTrue(all(item[0] == "GET" for item in self.fixture.backend.requests))

    def test_developer_resources_change_without_rebuilding_or_relay_writes(self):
        folder = self.developer_ui()
        self.assertIn(b'/__dev__/reload.js', self.request("/")[1])
        before = json.loads(self.request("/__dev__/state")[1])["build"]
        (folder / "app.js").write_text("const changed = true;", encoding="utf8")
        self.assertEqual(self.request("/app.js")[1], b"const changed = true;")
        self.assertNotEqual(json.loads(self.request("/__dev__/state")[1])["build"], before)
        self.assertEqual(self.fixture.backend.requests, [])
        script = self.request("/__dev__/reload.js")[1]
        self.assertIn(b"'F5'", script)
        self.assertIn(b"event.ctrlKey", script)
        self.assertIn(b"event.repeat", script)
        self.assertTrue(json.loads(self.request("/api/status")[1])["ui_live_reload"])

    def test_developer_route_keeps_local_origin_guard(self):
        self.developer_ui()
        self.assertEqual(self.request("/__dev__/state", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.fixture.backend.requests, [])

    def test_tray_resources_hot_reload_independently_of_main_window(self):
        folder = self.developer_ui()
        self.assertIn(b'/__dev__/reload.js?view=tray', self.request("/tray.html")[1])
        main = json.loads(self.request("/__dev__/state")[1])["build"]
        tray = json.loads(self.request("/__dev__/state?view=tray")[1])["build"]
        (folder / "tray.css").write_text("body{background:white}", encoding="utf8")
        self.assertEqual(self.request("/tray.css")[1], b"body{background:white}")
        self.assertEqual(json.loads(self.request("/__dev__/state")[1])["build"], main)
        self.assertNotEqual(json.loads(self.request("/__dev__/state?view=tray")[1])["build"], tray)
        self.assertIn(b'/__dev__/state?view=tray', self.request("/__dev__/reload.js?view=tray")[1])
        self.assertEqual(self.fixture.backend.requests, [])

    def test_missing_developer_file_reports_temporarily_unavailable(self):
        folder = self.developer_ui()
        (folder / "app.js").unlink()
        self.assertEqual(self.request("/app.js")[0], 503)
        self.assertEqual(self.request("/__dev__/state")[0], 503)
        self.assertEqual(self.fixture.backend.requests, [])


if __name__ == "__main__":
    unittest.main()
