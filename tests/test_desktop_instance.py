"""Repeated desktop launches must not start another relay or frontend."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from packet_inspector.desktop import SingleWindow
from packet_inspector.window_lifecycle import request_window_exit, request_tray_panel, tray_panel_host_running


@unittest.skipUnless(os.name == "nt", "Windows named window handles")
class DesktopInstanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_dev_and_release_processes_raise_existing_window_before_starting_service(self):
        owner = SingleWindow(self.folder)
        self.addCleanup(owner.close)
        self.assertFalse(owner.existing)
        launch = """
import sys
from pathlib import Path
from packet_inspector import desktop
from packet_inspector.frontend_dev import BuildProfile
mode = sys.argv[1]
desktop.load_profile = lambda: BuildProfile(mode, Path.cwd() / 'packet_inspector' / 'ui' if mode == 'dev' else None)
def unexpected_service(options):
    raise AssertionError('a repeated launch tried to start or attach a service')
desktop.ensure_service = unexpected_service
raise SystemExit(desktop.run(sys.argv[2:]))
"""
        for mode, port in [("release", 47844), ("dev", 47844), ("release", 47847), ("dev", 47847)]:
            with self.subTest(mode=mode, port=port):
                child = subprocess.run(
                    [sys.executable, "-c", launch, mode, "--data-dir", str(self.folder),
                     "--config", str(self.folder / "config.toml"), "--port", str(port)],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=25,
                )
                self.assertEqual(child.returncode, 0, child.stderr[-1500:])
                self.assertEqual(owner.kernel.WaitForSingleObject(owner.event, 1000), 0)

    def test_isolated_profiles_remain_independent_and_closing_releases_instance(self):
        first = SingleWindow(self.folder)
        other = SingleWindow(self.folder / "isolated")
        self.addCleanup(first.close)
        self.addCleanup(other.close)
        self.assertFalse(first.existing)
        self.assertFalse(other.existing)
        first.close()
        reopened = SingleWindow(self.folder)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.existing)

    def test_tray_panel_signal_does_not_show_main_window_or_start_another_service(self):
        owner = SingleWindow(self.folder)
        self.addCleanup(owner.close)
        script = "from packet_inspector.desktop import run; import sys; raise SystemExit(run(sys.argv[1:]))"
        result = subprocess.run([sys.executable, "-c", script, "--tray-panel", "--data-dir", str(self.folder),
                                 "--config", str(self.folder / "config.toml")],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        self.assertEqual(owner.kernel.WaitForSingleObject(owner.panel_event, 1000), 0)
        self.assertNotEqual(owner.kernel.WaitForSingleObject(owner.event, 0), 0)

    def test_preload_reuses_the_existing_host_without_opening_either_window(self):
        self.assertFalse(tray_panel_host_running(self.folder))
        owner = SingleWindow(self.folder)
        self.addCleanup(owner.close)
        self.assertTrue(tray_panel_host_running(self.folder))
        script = "from packet_inspector.desktop import run; import sys; raise SystemExit(run(sys.argv[1:]))"
        result = subprocess.run([sys.executable, "-c", script, "--tray-preload", "--data-dir", str(self.folder),
                                 "--config", str(self.folder / "config.toml")],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        self.assertNotEqual(owner.kernel.WaitForSingleObject(owner.panel_event, 0), 0)
        self.assertNotEqual(owner.kernel.WaitForSingleObject(owner.event, 0), 0)

    def test_upgrade_exit_signal_targets_only_the_same_profile(self):
        owner = SingleWindow(self.folder)
        self.addCleanup(owner.close)
        closed = threading.Event()
        watcher = threading.Thread(target=owner.watch, args=(lambda: None, closed.set), daemon=True)
        watcher.start()
        self.assertFalse(request_window_exit(self.folder / "other"))
        self.assertFalse(closed.wait(.1))
        self.assertTrue(request_window_exit(self.folder))
        self.assertTrue(closed.wait(2))
        watcher.join(2)

    def test_live_tray_panel_opens_by_event_without_launching_a_process_or_showing_main(self):
        owner = SingleWindow(self.folder)
        self.addCleanup(owner.close)
        opened, main = threading.Event(), threading.Event()
        watcher = threading.Thread(target=owner.watch, args=(main.set, None, opened.set), daemon=True)
        watcher.start()
        try:
            self.assertFalse(request_tray_panel(self.folder / "other"))
            self.assertTrue(request_tray_panel(self.folder))
            self.assertTrue(opened.wait(2))
            self.assertFalse(main.is_set())
            self.assertEqual(owner.activations, 0)
        finally:
            owner.stop.set()
            watcher.join(2)


if __name__ == "__main__":
    unittest.main()
