"""Managed Windows installs use verified files and preserve unknown additions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from packet_inspector import managed_runtime as runtime


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve()
        self.app = self.data / "bin" / "versions" / ("a" * 64) / runtime.APP_EXE
        self.app.parent.mkdir(parents=True)
        self.app.write_bytes(b"self-contained application")
        self.resources = self.data / "bundle"
        embedded = self.resources / runtime.EMBEDDED_RUNTIME
        embedded.mkdir(parents=True)
        self.loader = embedded / runtime.APP_EXE
        self.loader.write_bytes(b"installed executable")
        self.asset = self.resources / "packet_inspector" / "ui" / "index.html"
        self.asset.parent.mkdir()
        self.asset.write_bytes(b"captured frontend")
        self.marker = embedded / runtime.LAYOUT_FILE
        self.layout = {"version": 1, "exe": runtime.sha256_file(self.loader),
                       "files": {"packet_inspector/ui/index.html": runtime.sha256_file(self.asset)}}
        self.marker.write_text(json.dumps(self.layout), encoding="utf8")

    def test_install_retains_payload_and_reuses_verified_runtime(self):
        self.assertEqual(runtime.install_runtime(self.app), self.app)
        executable = runtime.install_runtime(self.app, self.resources)
        self.assertEqual(executable.read_bytes(), self.loader.read_bytes())
        self.assertEqual((executable.parent / runtime.LIBRARIES / "packet_inspector/ui/index.html").read_bytes(),
                         self.asset.read_bytes())
        self.assertEqual(runtime.application_payload(executable, self.data), self.app)
        self.assertEqual(runtime.install_runtime(self.app), executable)
        self.assertEqual(self.app.read_bytes(), b"self-contained application")

    def test_partial_install_is_never_published(self):
        self.asset.write_bytes(b"altered after verification")
        with self.assertRaisesRegex(ValueError, "failed verification"):
            runtime.install_runtime(self.app, self.resources)
        self.assertEqual(list(self.app.parent.iterdir()), [self.app])

    def test_layout_cannot_address_a_file_outside_its_directory(self):
        self.layout["files"]["../outside"] = "a" * 64
        self.marker.write_text(json.dumps(self.layout), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Invalid Windows runtime files"):
            runtime.install_runtime(self.app, self.resources)

    @unittest.skipUnless(sys.platform == "win32", "Windows path limit")
    def test_single_file_installation_still_supports_deep_paths(self):
        self.layout["files"]["long/" + "x" * 128] = "a" * 64
        self.marker.write_text(json.dumps(self.layout), encoding="utf8")
        self.assertEqual(runtime.install_runtime(self.app, self.resources), self.app)
        self.assertEqual(list(self.app.parent.iterdir()), [self.app])

    def test_retention_only_removes_unchanged_managed_files(self):
        executable = runtime.install_runtime(self.app, self.resources)
        extra = executable.parent / "keep.txt"
        extra.write_bytes(b"custom")
        self.assertFalse(runtime.remove_runtime(executable.parent))
        self.assertTrue(executable.is_file())
        extra.unlink()
        executable.write_bytes(b"custom")
        self.assertFalse(runtime.remove_runtime(executable.parent))
        executable.write_bytes(self.loader.read_bytes())
        self.assertTrue(runtime.remove_runtime(executable.parent))
        self.assertFalse(executable.parent.exists())
        self.assertTrue(self.app.is_file())
