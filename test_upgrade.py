"""Protect live services when a payload, endpoint, or post-switch check fails."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from packet_inspector.frontend_dev import DESKTOP_PROFILE
from packet_inspector.locking import file_lock
from packet_inspector.router import atomic_json
from packet_inspector.upgrade import Upgrade


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload, self.data = self.root / "payload", self.root / "data"
        self.payload.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text('model="fixture"\n', encoding="utf8")
        hashes = {}
        for name in ("CodexPacketInspector.exe",):
            raw = (name + "-verified").encode()
            (self.payload / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        atomic_json(self.payload / "manifest.json", {"version": "9.0.0", "router_protocol": 1,
                                                     "entry_mode": "shared", "sha256": hashes})
        self.engine = Upgrade(self.payload, self.data, self.config, 47844)
        self.engine._start_candidate = Mock(side_effect=lambda *_args: setattr(self.engine, "candidate", "2" * 32))
        self.engine._stop_candidate = Mock()
        self.engine._application_windows = Mock(return_value=[])

    def health(self, _port, path, data=None, *_args, **_kwargs):
        if path == "/health":
            return {"app": "codex-packet-inspector", "router_protocol": 1}
        if path == "/api/status":
            return {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}
        return {}

    def test_corrupted_payload_never_starts_or_stops_a_service(self):
        (self.payload / "CodexPacketInspector.exe").write_bytes(b"tampered")
        with patch("packet_inspector.upgrade.local_json") as api:
            with self.assertRaisesRegex(ValueError, "校验失败"):
                self.engine.run()
        api.assert_not_called()
        self.engine._start_candidate.assert_not_called()
        self.engine._stop_candidate.assert_not_called()
        self.assertEqual(json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8"))["state"], "failed")

    def test_unresponsive_but_occupied_entry_is_never_migrated(self):
        with patch("packet_inspector.upgrade.local_json", side_effect=TimeoutError), \
                patch("packet_inspector.upgrade.socket.socket") as sockets:
            sockets.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "未替换入口"):
                self.engine.run()
        self.engine._start_candidate.assert_not_called()

    def test_unknown_entry_mode_never_touches_the_running_service(self):
        path = self.payload / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        manifest["entry_mode"] = "unknown"
        atomic_json(path, manifest)
        with patch("packet_inspector.upgrade.local_json") as api:
            with self.assertRaisesRegex(ValueError, "入口模式不兼容"):
                self.engine.run()
        api.assert_not_called()
        self.engine._start_candidate.assert_not_called()

    def test_old_separate_entry_payload_remains_compatible(self):
        path = self.payload / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        manifest.pop("entry_mode")
        raw = b"legacy-entry-payload"
        (self.payload / "CodexInspectorRouter.exe").write_bytes(raw)
        manifest["sha256"]["CodexInspectorRouter.exe"] = hashlib.sha256(raw).hexdigest()
        atomic_json(path, manifest)
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32})
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health):
            self.engine.run()
        self.engine._start_candidate.assert_called_once()

    def test_post_switch_failure_switches_back_before_stopping_candidate(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(side_effect=RuntimeError("post-switch failure"))
        calls = []
        def api(port, path, data=None, *args, **kwargs):
            if path == "/__router__/switch":
                calls.append(data)
            return self.health(port, path, data, *args, **kwargs)
        with patch("packet_inspector.upgrade.local_json", side_effect=api):
            with self.assertRaisesRegex(RuntimeError, "post-switch failure"):
                self.engine.run()
        self.assertEqual(calls, [{"worker_id": "2" * 32, "expected_active": "1" * 32},
                                 {"worker_id": "1" * 32, "expected_active": "2" * 32}])
        self.engine._stop_candidate.assert_called_once()

    def test_desktop_window_is_closed_and_reopened_even_if_switch_fails(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        windows = [{"pid": 123, "background": False}]
        self.engine._application_windows.return_value = windows
        def api(port, path, data=None, *args, **kwargs):
            if path == "/__router__/switch":
                raise ValueError("candidate activation failed")
            return self.health(port, path, data, *args, **kwargs)
        with patch("packet_inspector.upgrade.local_json", side_effect=api), \
                patch.object(self.engine, "_close_windows") as close, \
                patch.object(self.engine, "_open_window") as reopen:
            with self.assertRaisesRegex(ValueError, "candidate activation"):
                self.engine.run()
        close.assert_called_once_with(windows)
        reopen.assert_called_once()
        self.engine._stop_candidate.assert_called_once()

    def test_second_upgrader_cannot_modify_running_upgrade_result(self):
        atomic_json(self.data / "upgrade-result.json", {"state": "running", "started": 123})
        with file_lock(self.data / "upgrade.lock"):
            with self.assertRaises(BlockingIOError):
                self.engine.run()
        self.assertEqual(json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8")), {"state": "running", "started": 123})
        self.engine._start_candidate.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows process discovery")
class LegacyWindowDiscoveryTests(unittest.TestCase):
    def test_default_launches_are_found_without_including_custom_profiles_or_backgrounds(self):
        compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            self.skipTest("Windows .NET compiler unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source = root / "LegacyWindowFixture.cs"
        source.write_text("class LegacyWindowFixture { static void Main() { System.Threading.Thread.Sleep(60000); } }", encoding="utf8")
        formal, development = root / "CodexPacketInspector.exe", root / "CodexPacketInspector-Dev.exe"
        compiled = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(formal), str(source)],
                                  capture_output=True, text=True, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        shutil.copy2(formal, development)
        data = root / ".codex-packet-inspector"
        engine = Upgrade(root, data, root / "config.toml")
        processes = []
        def launch(executable, *arguments):
            process = subprocess.Popen([str(executable), *map(str, arguments)], creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            return process.pid
        def stop_fixtures():
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
        self.addCleanup(stop_fixtures)
        expected = {launch(formal), launch(development), launch(formal, "--data-dir", data)}
        background = launch(formal, "--background")
        excluded = {launch(formal, "--data-dir", root / "another-profile"), background}
        # A stale foreground identity must not turn a background into a GUI.
        atomic_json(data / DESKTOP_PROFILE, {"mode": "release", "pid": background, "executable": str(formal)})
        with patch("packet_inspector.upgrade.Path.home", return_value=root):
            found = {item["pid"]: item for item in engine._application_windows()}
        self.assertTrue(expected <= found.keys())
        self.assertTrue(excluded.isdisjoint(found))
        self.assertEqual({found[pid]["mode"] for pid in expected}, {"release", "dev"})


if __name__ == "__main__":
    unittest.main()
