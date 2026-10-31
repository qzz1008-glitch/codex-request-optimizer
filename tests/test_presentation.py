import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector.presentation import WorkerPresentation
from packet_inspector.router import atomic_json, worker_info


class PresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.config = self.data / "config.toml"
        self.config.write_text('model="test"', encoding="utf8")
        self.identity, self.other = "1" * 32, "2" * 32
        self.server = SimpleNamespace(data_dir=self.data, link=SimpleNamespace(config=self.config),
                                      worker_id=self.identity, worker_state="standby", public_port=47844,
                                      tray_status=Mock(return_value={}))
        self.factory, self.retire = Mock(), Mock()
        self.tray = self.factory.return_value.start.return_value
        self.tray.thread.is_alive.return_value = True
        self.presentation = WorkerPresentation(self.server, enabled=True, tray_factory=self.factory, retire_entry=self.retire)
        self.addCleanup(self.presentation.stop)
        preload = patch.object(self.presentation, "_preload_panel")
        self.preload = preload.start()
        self.addCleanup(preload.stop)
        atomic_json(self.data / "runtime.json", {"pid": 98765})
        self.worker(self.identity)
        self.worker(self.other)
        self.active(self.identity)

    def worker(self, identity, committed=False):
        executable = self.data / "bin" / "versions" / identity / "CodexPacketInspector.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"synthetic")
        atomic_json(self.data / "router-workers" / (identity + ".json"),
                    {"id": identity, "port": 49123, "token": "test", "version": "test", "desktop_tray": True,
                     "executable": str(executable), "presentation_committed": committed})
        return executable

    def active(self, identity):
        atomic_json(self.data / "router-state.json", {"active": identity, "config": str(self.config)})

    def test_standby_cannot_replace_the_existing_tray(self):
        self.presentation.reconcile()
        self.factory.assert_not_called()
        self.retire.assert_not_called()
        with self.assertRaisesRegex(ValueError, "active worker"):
            self.presentation.commit()

    def test_new_tray_is_ready_and_persisted_before_the_entry_tray_is_closed(self):
        self.server.worker_state = "active"
        def retire(*args):
            self.assertTrue(self.server.desktop_tray)
            self.assertTrue(worker_info(self.data, self.identity)["presentation_committed"])
        self.retire.side_effect = retire
        self.presentation.commit()
        self.retire.assert_called_with(98765, self.data / "bin" / "CodexPacketInspector.exe")
        self.assertTrue(self.server.desktop_tray)

    def test_restart_uses_the_committed_worker_icon(self):
        self.worker(self.identity, committed=True)
        self.server.worker_state = "active"
        self.presentation.reconcile()
        self.factory.assert_called_once_with(self.presentation._open_window, self.presentation._exit,
                                             status_reader=self.server.tray_status, show_panel=self.presentation._show_panel,
                                             restart_service=self.presentation._restart_service)
        self.retire.assert_called_once()

    def test_old_tray_stays_until_the_next_tray_commits_and_survives_rollback(self):
        self.server.worker_state = "active"
        self.presentation.commit()
        self.server.worker_state = "draining"
        self.active(self.other)
        self.presentation.reconcile()
        self.tray.stop.assert_not_called()
        self.active(self.identity)
        self.server.worker_state = "active"
        self.presentation.reconcile()
        self.tray.stop.assert_not_called()
        self.active(self.other)
        self.server.worker_state = "draining"
        self.worker(self.other, committed=True)
        self.presentation.reconcile()
        self.tray.stop.assert_called_once()
        self.assertFalse(self.server.desktop_tray)

    def test_native_start_failure_does_not_close_the_old_tray(self):
        self.server.worker_state = "active"
        self.factory.return_value.start.side_effect = RuntimeError("native failure")
        with self.assertRaisesRegex(RuntimeError, "native failure"):
            self.presentation.commit()
        self.retire.assert_not_called()
        self.assertFalse(worker_info(self.data, self.identity)["presentation_committed"])

    def test_persistence_failure_removes_only_the_new_tray(self):
        self.server.worker_state = "active"
        with patch("packet_inspector.presentation.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.presentation.commit()
        self.retire.assert_not_called()
        self.tray.stop.assert_called_once()

    def test_tray_opens_the_current_worker_instead_of_its_own_old_version(self):
        self.active(self.other)
        target = self.worker(self.other)
        with patch("packet_inspector.presentation.subprocess.Popen") as launch:
            self.presentation._open_window()
        self.assertEqual(launch.call_args.args[0][0], str(target.resolve()))
        self.assertIn(str(self.config), launch.call_args.args[0])
        self.assertNotIn("--tray-panel", launch.call_args.args[0])

    def test_panel_uses_the_current_worker_and_keeps_the_main_window_hidden(self):
        self.active(self.other)
        target = self.worker(self.other)
        with patch("packet_inspector.presentation.subprocess.Popen") as launch:
            self.presentation._show_panel()
        self.assertEqual(launch.call_args.args[0][0], str(target.resolve()))
        self.assertIn("--tray-panel", launch.call_args.args[0])
        self.assertIn(str(self.data), launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_ready_panel_is_signaled_directly_without_starting_another_exe(self):
        with patch("packet_inspector.window_lifecycle.request_tray_panel", return_value=True) as signal, \
                patch("packet_inspector.presentation.subprocess.Popen") as launch:
            self.presentation._show_panel()
        signal.assert_called_once_with(self.data)
        launch.assert_not_called()

    def test_committed_tray_warms_the_host_once_and_never_during_validation(self):
        import threading
        warmed = threading.Event()
        self.preload.side_effect = warmed.set
        self.presentation.reconcile()
        self.preload.assert_not_called()
        self.server.worker_state = "active"
        self.presentation.commit()
        self.assertTrue(warmed.wait(1))
        self.presentation.reconcile()
        self.preload.assert_called_once()

    def test_warmup_starts_a_hidden_current_version_and_reuses_an_existing_host(self):
        self.active(self.other)
        target = self.worker(self.other)
        with patch("packet_inspector.window_lifecycle.tray_panel_host_running", return_value=False), \
                patch("packet_inspector.presentation.subprocess.Popen") as launch:
            WorkerPresentation._preload_panel(self.presentation)
        self.assertEqual(launch.call_args.args[0][0], str(target.resolve()))
        self.assertIn("--tray-preload", launch.call_args.args[0])
        self.assertNotIn("--tray-panel", launch.call_args.args[0])
        with patch("packet_inspector.window_lifecycle.tray_panel_host_running", return_value=True), \
                patch("packet_inspector.presentation.subprocess.Popen") as launch:
            WorkerPresentation._preload_panel(self.presentation)
        launch.assert_not_called()

    def test_foreign_executable_and_profile_cannot_be_launched_or_retired(self):
        path = self.data / "router-workers" / (self.identity + ".json")
        value = json.loads(path.read_text(encoding="utf8"))
        atomic_json(path, {**value, "executable": str(self.config)})
        with patch("packet_inspector.presentation.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "managed versions"):
                self.presentation._open_window()
        launch.assert_not_called()
        atomic_json(self.data / "router-state.json", {"active": self.identity, "config": str(self.data / "foreign")})
        self.server.worker_state = "active"
        with self.assertRaisesRegex(ValueError, "profile mismatch"):
            self.presentation.commit()
        self.retire.assert_not_called()
