"""One frozen application must keep installed entries separate from workers."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector import launch


class LaunchRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.state = self.data / "router-state.json"
        self.state.write_text("{}", encoding="utf8")
        self.canonical = self.data / "bin" / "CodexPacketInspector.exe"
        self.arguments = ["--data-dir", str(self.data), "--port", "47844"]

    def test_installed_entry_preserves_background_and_tray_arguments(self):
        arguments = ["--background", "--tray", *self.arguments]
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(self.canonical)), \
                patch("packet_inspector.router.main", return_value=7) as entry, \
                patch.object(launch, "service_main") as service:
            self.assertEqual(launch.main(arguments), 7)
        entry.assert_called_once_with(arguments)
        service.assert_not_called()

    def test_foreground_installed_entry_uses_router_window_launch(self):
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(self.canonical)), \
                patch("packet_inspector.router.main", return_value=0) as entry, \
                patch("packet_inspector.desktop.run") as desktop:
            self.assertEqual(launch.main(self.arguments), 0)
        entry.assert_called_once_with(self.arguments)
        desktop.assert_not_called()

    def test_versioned_workers_never_take_over_the_stable_entry(self):
        executable = self.data / "bin" / "versions" / "fixture" / "CodexPacketInspector.exe"
        arguments = ["--worker-id", "a" * 32, *self.arguments]
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(executable)), \
                patch("packet_inspector.router.main") as entry, \
                patch.object(launch, "service_main", return_value=0) as service:
            self.assertEqual(launch.main(["--background", *arguments]), 0)
        entry.assert_not_called()
        service.assert_called_once_with(arguments, on_started=None)

    def test_unmigrated_installation_still_starts_as_legacy_service(self):
        self.state.unlink()
        with patch.object(launch.sys, "frozen", True, create=True), \
                patch.object(launch.sys, "executable", str(self.canonical)), \
                patch("packet_inspector.router.main") as entry, \
                patch.object(launch, "service_main", return_value=0) as service:
            self.assertEqual(launch.main(["--background", *self.arguments]), 0)
        entry.assert_not_called()
        service.assert_called_once_with(self.arguments, on_started=None)

    def test_source_and_uninstalled_app_keep_normal_startup(self):
        for frozen, executable in [(False, self.canonical), (True, self.root / "download.exe")]:
            with self.subTest(frozen=frozen), \
                    patch.object(launch.sys, "frozen", frozen, create=True), \
                    patch.object(launch.sys, "executable", str(executable)), \
                    patch("packet_inspector.router.main") as entry, \
                    patch.object(launch, "service_main", return_value=0):
                self.assertEqual(launch.main(["--background", *self.arguments]), 0)
            entry.assert_not_called()

    def test_tray_exit_opens_confirmation_without_stopping_service(self):
        callbacks = {}
        tray = SimpleNamespace(start=lambda: tray, stop=lambda: None)

        def native_tray(open_window, exit_app, *, status_reader=None, show_panel=None):
            callbacks.update(open=open_window, exit=exit_app, status_reader=status_reader, show_panel=show_panel)
            return tray

        def service(arguments, on_started):
            server = SimpleNamespace(port=47844, tray_status=Mock(return_value={}))
            on_started(server)
            callbacks["exit"]()
            callbacks["show_panel"]()
            self.assertIs(callbacks["status_reader"], server.tray_status)
            self.assertTrue(server.desktop_tray)
            return 0

        with patch.object(launch.sys, "frozen", False, create=True), \
                patch.object(launch, "service_main", side_effect=service), \
                patch("packet_inspector.tray.NativeTray", side_effect=native_tray), \
                patch("packet_inspector.desktop.start_process") as start, \
                patch("packet_inspector.exit_flow.request_exit") as request, \
                patch("packet_inspector.desktop.local_api") as api:
            self.assertEqual(launch.main(["--background", "--tray", *self.arguments]), 0)
        request.assert_called_once_with(self.data, callbacks["open"])
        self.assertIn("--tray-panel", start.call_args.args[0])
        self.assertNotIn("--background", start.call_args.args[0])
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
