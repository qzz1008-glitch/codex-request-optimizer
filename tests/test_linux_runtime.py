"""Linux service startup and platform boundaries, without touching a live profile."""
from pathlib import Path
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector import launch
from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig
from packet_inspector.updates import Updates
from packet_inspector.server import Handler
from scripts.build_inspector import LINUX_FILES, package_release


class LinuxRuntimeTests(unittest.TestCase):
    def test_explicit_ssh_port_allows_only_loopback_browser_origins(self):
        server = SimpleNamespace(port=47844, public_port=47844, ui_forward_port=47848)
        handler = SimpleNamespace(server=server, headers={"Host": "127.0.0.1:47848", "Origin": "http://localhost:47848"})
        self.assertTrue(Handler._valid_host(handler))
        self.assertTrue(Handler._valid_origin(handler))
        for name, value in (("Host", "example.com:47848"), ("Host", "localhost:47849"),
                            ("Origin", "http://example.com:47848"), ("Origin", "http://localhost:47849")):
            with self.subTest(name=name, value=value):
                baseline = dict(handler.headers)
                handler.headers[name] = value
                self.assertFalse((Handler._valid_host if name == "Host" else Handler._valid_origin)(handler))
                handler.headers = baseline
        server.ui_forward_port = None
        self.assertFalse(Handler._valid_host(handler))
        self.assertFalse(Handler._valid_origin(handler))

    def test_sigterm_waits_for_active_requests_before_shutdown_and_restores_handler(self):
        stopped, handlers = threading.Event(), {}
        server = SimpleNamespace(guard=threading.RLock(), active_requests=1, upgrade_draining=False,
                                 shutdown=stopped.set)
        def service(arguments, on_started):
            on_started(server)
            handlers[launch.signal.SIGTERM](launch.signal.SIGTERM, None)
            deadline = time.monotonic() + 2
            while not server.upgrade_draining and time.monotonic() < deadline:
                stopped.wait(.01)
            self.assertTrue(server.upgrade_draining)
            self.assertFalse(stopped.is_set())
            with server.guard:
                server.active_requests = 0
            self.assertTrue(stopped.wait(2))
            return 0
        with patch.object(launch.signal, "getsignal", return_value="original"), \
                patch.object(launch.signal, "signal", side_effect=lambda number, value: handlers.update({number: value})), \
                patch.object(launch, "service_main", side_effect=service):
            self.assertEqual(launch.console_service([]), 0)
        self.assertEqual(handlers[launch.signal.SIGTERM], "original")

    def test_linux_default_and_background_start_the_service_without_desktop_imports(self):
        for flags in ([], ["--background"]):
            with self.subTest(flags=flags), patch.object(launch.sys, "platform", "linux"), \
                    patch.object(launch, "console_service", return_value=0) as start:
                self.assertEqual(launch.main([*flags, "--port", "47849"]), 0)
                start.assert_called_once_with(["--port", "47849"])

    def test_linux_rejects_windows_tray_role_without_starting_the_service(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch.sys, "platform", "linux"), \
                patch.object(launch, "console_service") as start:
            self.assertEqual(launch.main(["--tray", "--data-dir", folder]), 1)
            start.assert_not_called()
            self.assertIn("Linux", (Path(folder) / "startup-error.txt").read_text(encoding="utf8"))

    def test_windows_background_still_uses_existing_service_entry(self):
        with patch.object(launch.sys, "platform", "win32"), \
                patch.object(launch, "service_main", return_value=0) as start, \
                patch.object(launch, "console_service") as linux:
            self.assertEqual(launch.main(["--background", "--port", "47849"]), 0)
            start.assert_called_once_with(["--port", "47849"], on_started=None)
            linux.assert_not_called()

    def test_unsupported_image_optimization_cannot_change_config_or_install_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "codex" / "config.toml"
            config.parent.mkdir()
            before = b'model = "gpt-6.1-sol"\n'
            config.write_bytes(before)
            manager = ImageToolConfig(ConfigLink(config, root / "data"), root / "data", lambda: "http://127.0.0.1:47844")
            manager.installation.supported = False
            self.assertEqual(tuple(manager.available_modes()), ("native", "disabled"))
            with self.assertRaisesRegex(ValueError, "Linux"):
                with manager.change("optimized"):
                    self.fail("unsupported mode was applied")
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(manager.installation.directory.exists())
            self.assertFalse(manager.path.exists())

    def test_unsupported_updater_never_downloads_or_runs_a_windows_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = Updates(folder, Path(folder) / "config.toml", 47844, opener=Mock())
            manager.supported = False
            self.assertFalse(manager.status()["supported"])
            manager.poll()
            for action in (manager.check, manager.install, lambda: manager.start("check"),
                           lambda: manager.configure({"automatic": True})):
                with self.assertRaisesRegex(ValueError, "Linux"):
                    action()
            manager.opener.open.assert_not_called()
            self.assertIsNone(manager.task)

    def test_linux_archive_contains_only_usable_files_and_executable_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, stage, licenses = (root / name for name in ("source", "stage", "licenses"))
            for p in (source / "docs", source / "scripts" / "linux", stage, licenses):
                p.mkdir(parents=True)
            for name in ("README.md", "LICENSE", "docs/README.en.md", "docs/THIRD_PARTY_NOTICES.txt",
                         "scripts/linux/codex-packet-inspector.service"):
                (source / name).write_text(name, encoding="utf8")
            payload = b"ELF fixture\n"
            (stage / "CodexPacketInspector").write_bytes(payload)
            (stage / "startup-error.log").write_text("private")
            with patch("scripts.build_inspector.sys.platform", "linux"), \
                    patch("scripts.build_inspector.platform.machine", return_value="x86_64"):
                package_release(source, stage, licenses)
            with tarfile.open(stage / "CodexPacketInspector-linux-x86_64.tar.gz") as archive:
                self.assertEqual(set(archive.getnames()), {"codex-packet-inspector/" + name for name in LINUX_FILES})
                for member in archive:
                    self.assertTrue(member.isfile())
                    self.assertEqual(member.mode, 0o755 if member.name.endswith("/CodexPacketInspector") else 0o644)
                self.assertEqual(archive.extractfile("codex-packet-inspector/CodexPacketInspector").read(), payload)


if __name__ == "__main__":
    unittest.main()
