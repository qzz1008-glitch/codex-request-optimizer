"""Repeated desktop launches must not start another relay or frontend."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from packet_inspector.desktop import SingleWindow


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
                    cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=25,
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


if __name__ == "__main__":
    unittest.main()
