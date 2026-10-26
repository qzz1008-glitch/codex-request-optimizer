import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.router import atomic_json
from packet_inspector.upgrade_cleanup import GRACE_SECONDS, cleanup_versions


class UpgradeCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve()
        self.versions = self.data / "bin" / "versions"
        self.now = 2000000
        self.active, self.draining = "1" * 32, "2" * 32
        self.paths = [self.version(i, age=GRACE_SECONDS + 100 + i) for i in range(8)]
        self.worker(self.active, self.paths[7])
        self.worker(self.draining, self.paths[6])
        self.routes()

    def version(self, number, age):
        directory = self.versions / format(number, "064x")
        directory.mkdir(parents=True)
        (directory / "CodexPacketInspector.exe").write_bytes(b"synthetic application")
        os.utime(directory, (self.now - age, self.now - age))
        return directory

    def worker(self, identity, path):
        atomic_json(self.data / "router-workers" / (identity + ".json"),
                    {"id": identity, "port": 49123, "version": "test", "token": "test",
                     "executable": str(path / "CodexPacketInspector.exe")})

    def routes(self):
        atomic_json(self.data / "router-state.json", {"active": self.active, "draining": [{"id": self.draining}]})

    def test_preserve_active_draining_two_rollback_copies_and_shared_data(self):
        for name in ["packets.sqlite", "settings.json", "codex-link.json"]:
            (self.data / name).write_bytes(b"shared data")
        self.worker("3" * 32, self.paths[5])
        result = cleanup_versions(self.data, now=self.now)
        self.assertEqual(result["versions_removed"], 4)
        self.assertEqual(result["worker_records_removed"], 1)
        self.assertEqual([p.exists() for p in self.paths], [True, True, False, False, False, False, True, True])
        for name in ["packets.sqlite", "settings.json", "codex-link.json"]:
            self.assertEqual((self.data / name).read_bytes(), b"shared data")

    def test_live_worker_and_desktop_versions_are_protected(self):
        self.worker("3" * 32, self.paths[4])
        atomic_json(self.data / "router-workers" / ("3" * 32 + ".runtime.json"), {"pid": 1234})
        atomic_json(self.data / "desktop-profile.json", {"mode": "release", "executable": str(self.paths[5] / "CodexPacketInspector.exe")})
        with patch("packet_inspector.upgrade_cleanup.process_alive", return_value=True):
            cleanup_versions(self.data, now=self.now)
        self.assertTrue(self.paths[4].exists())
        self.assertTrue(self.paths[5].exists())

    def test_recent_failed_payloads_and_unrecognized_files_are_preserved(self):
        fresh = self.version(99, age=30)
        (self.paths[3] / "my-notes.txt").write_text("keep", encoding="utf8")
        os.utime(self.paths[3], (1, 1))
        cleanup_versions(self.data, now=self.now)
        self.assertTrue(fresh.exists())
        self.assertTrue((self.paths[3] / "my-notes.txt").exists())

    def test_invalid_metadata_fails_closed(self):
        (self.data / "router-state.json").write_text("incomplete", encoding="utf8")
        result = cleanup_versions(self.data, now=self.now)
        self.assertIn("skipped", result)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_locked_executable_does_not_fail_upgrade_or_delete_its_descriptor(self):
        self.worker("3" * 32, self.paths[5])
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.parent == self.paths[5]:
                raise PermissionError("still loaded")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            result = cleanup_versions(self.data, now=self.now)
        self.assertNotIn("skipped", result)
        self.assertTrue(self.paths[5].exists())
        self.assertTrue((self.data / "router-workers" / ("3" * 32 + ".json")).exists())

    def test_route_is_rechecked_before_each_deletion(self):
        from packet_inspector.upgrade_cleanup import _json
        calls = 0
        self.worker("3" * 32, self.paths[3])
        def read(path):
            nonlocal calls
            result = _json(path)
            if path.name == "router-state.json":
                calls += 1
                if calls > 1:
                    result["draining"].append({"id": "3" * 32})
            return result
        with patch("packet_inspector.upgrade_cleanup._json", side_effect=read):
            cleanup_versions(self.data, now=self.now)
        self.assertTrue(self.paths[3].exists())

    def test_old_downloads_are_bounded_without_removing_the_running_updater_or_user_files(self):
        folders = []
        for number in range(6):
            path = self.data / "updates" / ("1.0." + str(number))
            path.mkdir(parents=True)
            executable = path / "CodexPacketInspector.exe"
            executable.write_bytes(b"synthetic download")
            stamp = self.now - GRACE_SECONDS - 100 - number
            os.utime(executable, (stamp, stamp))
            os.utime(path, (stamp, stamp))
            folders.append(path)
        (folders[4] / "user-notes.txt").write_text("keep", encoding="utf8")
        os.utime(folders[4], (1, 1))
        os.utime(folders[5] / "CodexPacketInspector.exe", (self.now, self.now))
        with patch("packet_inspector.upgrade_cleanup.sys.executable", str(folders[3] / "CodexPacketInspector.exe")):
            result = cleanup_versions(self.data, now=self.now)
        self.assertEqual(result["downloads_removed"], 1)
        self.assertEqual([path.exists() for path in folders], [True, True, False, True, True, True])
