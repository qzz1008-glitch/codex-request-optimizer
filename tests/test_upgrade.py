"""Protect live services when a payload, endpoint, or post-switch check fails."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from packet_inspector.frontend_dev import DESKTOP_PROFILE
from packet_inspector.locking import file_lock
from packet_inspector.router import atomic_json
from packet_inspector.upgrade import Upgrade


class UpgradeTests(unittest.TestCase):
    def test_old_shared_service_is_preserved_before_candidate_or_config_changes(self):
        scenarios = ({"host": {"running": True}}, {"client": {"active": True}},
                     {"egress": {"host": {"active": True}}}, {"egress": {"selection": {"mode": "peer"}}})
        for legacy, shared in ((legacy, shared) for legacy in (False, True) for shared in scenarios):
            with self.subTest(legacy=legacy, shared=shared):
                before = self.config.read_bytes()
                def old_service(_port, path, **_kwargs):
                    if path == "/health":
                        return {"app": "codex-packet-inspector", **({} if legacy else {"router_protocol": 1})}
                    if path == "/api/status":
                        return {"data_dir": str(self.data), "profile": {"config_path": str(self.config)},
                                "sharing_settings_supported": True, "upgrade_drain_supported": True}
                    if path == "/api/sharing":
                        return shared
                    self.fail("The old service must not be stopped")
                with patch("packet_inspector.upgrade.local_json", side_effect=old_service):
                    with self.assertRaisesRegex(ValueError, "不支持共享无缝交接"):
                        self.engine.run()
                self.engine._start_candidate.assert_not_called()
                self.control.assert_not_called()
                self.assertEqual(self.config.read_bytes(), before)

    def test_sharing_handover_precedes_switch_and_finishes_before_candidate_cleanup(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
                calls = []
                def api(port, path, data=None, *args, **kwargs):
                    if path == "/__router__/switch":
                        calls.append("switch:" + data["worker_id"][0])
                    value = self.health(port, path, data, *args, **kwargs)
                    if path == "/api/status":
                        value.update(sharing_settings_supported=True, sharing_handover_protocol=1,
                                     sharing_runtime={"quota_out": True})
                    return value
                self.control.side_effect = lambda *_args, **_kwargs: calls.append("refresh")
                self.engine._stop_candidate.side_effect = lambda: calls.append("stop")
                self.engine._wait_entry = (Mock(side_effect=RuntimeError("post-switch failure")) if failed
                                           else Mock(return_value={"worker_id": "2" * 32,
                                                                   "sharing_runtime": {"quota_out": True}}))
                with patch("packet_inspector.upgrade.local_json", side_effect=api), \
                     patch("packet_inspector.upgrade.prepare_upgrade", side_effect=lambda *_args: calls.append("prepare") or "ticket") as prepare, \
                     patch("packet_inspector.upgrade.finish_upgrade", side_effect=lambda *_args: calls.append("finish")) as finish:
                    if failed:
                        with self.assertRaisesRegex(RuntimeError, "post-switch failure"):
                            self.engine.run()
                    else:
                        self.engine.run()
                prepare.assert_called_once_with(self.data.resolve(), "1" * 32, "2" * 32)
                finish.assert_called_once_with(self.data.resolve(), "1" * 32, "2" * 32, "ticket")
                self.assertEqual(calls, ["refresh", "prepare", "switch:2", "switch:1", "finish", "stop"] if failed
                                 else ["refresh", "prepare", "switch:2", "refresh", "finish"])

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
        control = patch("packet_inspector.upgrade.control_worker")
        self.control = control.start()
        self.addCleanup(control.stop)

    def health(self, _port, path, data=None, *_args, **_kwargs):
        if path == "/health":
            return {"app": "codex-packet-inspector", "router_protocol": 1}
        if path == "/api/status":
            return {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}
        if path == "/__router__/switch":
            state_path = self.data / "router-state.json"
            state = json.loads(state_path.read_text(encoding="utf8"))
            state["active"] = data["worker_id"]
            atomic_json(state_path, state)
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

    def test_routed_upgrade_refreshes_installation_then_commits_the_new_tray(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32, "desktop_refresh_supported": True})
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health):
            self.engine.run()
        self.assertEqual([call.args[2] for call in self.control.call_args_list],
                         ["upgrade_prepare", "installation_refresh", "desktop_refresh"])
        self.engine._stop_candidate.assert_not_called()

    def test_progress_reaches_complete_only_after_cleanup_and_result_are_saved(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32, "desktop_refresh_supported": True})
        events = []
        def progress(message):
            events.append(self.engine.completed_steps)
            if self.engine.completed_steps == self.engine.total_steps:
                result = json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8"))
                self.assertEqual(result["state"], "complete")
                self.assertIn("cleanup", result)
        self.engine.progress = progress
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health):
            self.engine.run()
        self.assertEqual(list(dict.fromkeys(events)), list(range(self.engine.total_steps + 1)))
        self.assertEqual(events, sorted(events))

    def test_cleanup_failure_preserves_success_and_the_active_service(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32})
        events = []
        self.engine.progress = lambda message: events.append(self.engine.completed_steps)
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health), \
                patch("packet_inspector.upgrade_cleanup.cleanup_versions", side_effect=OSError("fixture cleanup failure")):
            result = self.engine.run()
        self.assertEqual(events[-1], self.engine.total_steps)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["cleanup_warning"], "OSError")
        self.assertEqual(json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8"))["state"], "complete")
        self.engine._stop_candidate.assert_not_called()

    def test_backup_is_complete_before_a_candidate_can_touch_configuration(self):
        settings = self.data / "settings.json"
        settings.parent.mkdir(parents=True)
        atomic_json(settings, {"relay_token": "keep", "image_analysis_mode": "native"})
        tools = self.config.parent / "tools" / "codex-inspector-image-tool"
        tools.mkdir(parents=True)
        (tools / "server.ps1").write_text("original tool", encoding="utf8")
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        def start(*_args):
            backups = list((self.data / "upgrade-backups").glob("*/manifest.json"))
            self.assertEqual(len(backups), 1)
            rows = json.loads(backups[0].read_text(encoding="utf8"))
            for path in (self.config, settings, tools / "server.ps1"):
                row = next(row for row in rows if Path(row["path"]).resolve() == path.resolve())
                self.assertEqual(Path(row["saved"]).read_bytes(), path.read_bytes())
            self.assertNotIn(str(self.engine.canonical), [row["path"] for row in rows])
            self.engine.candidate = "2" * 32
        self.engine._start_candidate.side_effect = start
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32})
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health):
            self.engine.run()

    def test_failed_compatibility_preparation_never_switches_or_changes_user_settings(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        before = self.config.read_bytes()
        self.control.side_effect = ValueError("compatibility validation failed")
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health) as api:
            with self.assertRaisesRegex(ValueError, "compatibility validation failed"):
                self.engine.run()
        self.assertFalse(any(call.args[1] == "/__router__/switch" for call in api.call_args_list))
        self.assertEqual(self.config.read_bytes(), before)
        report = json.loads((self.data / "upgrade-result.json").read_text(encoding="utf8"))
        self.assertEqual(report["phase"], "compatibility")
        self.assertEqual(report["phases"][-1]["state"], "failed")

    def test_changed_connection_or_borrowing_state_aborts_handover_and_restores_old_owner(self):
        baseline = {"connection_state": "connected", "sharing_runtime": {"quota_in": True, "network_in": True}}
        for changed in ({"connection_state": "disconnected"}, {"sharing_runtime": {"quota_in": False}}):
            with self.subTest(changed=changed):
                atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
                def api(port, path, data=None, *args, **kwargs):
                    value = self.health(port, path, data, *args, **kwargs)
                    return {**value, **baseline} if path == "/api/status" else value
                self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32, **baseline, **changed})
                with patch("packet_inspector.upgrade.local_json", side_effect=api):
                    with self.assertRaisesRegex(ValueError, "服务状态不一致"):
                        self.engine.run()
                self.assertEqual(json.loads((self.data / "router-state.json").read_text(encoding="utf8"))["active"], "1" * 32)

    def test_post_commit_bookkeeping_failure_keeps_upgrade_successful(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32})
        def write(path, value):
            if Path(path).name == "upgrade-result.json" and value.get("state") == "complete":
                raise OSError("fixture storage failure")
            return atomic_json(path, value)
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health), \
                patch("packet_inspector.upgrade.atomic_json", side_effect=write):
            result = self.engine.run()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["finalization_warning"], "OSError")
        self.engine._stop_candidate.assert_not_called()

    def test_failed_native_refresh_rolls_back_before_retiring_the_old_tray(self):
        atomic_json(self.data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
        self.engine._wait_entry = Mock(return_value={"worker_id": "2" * 32, "desktop_refresh_supported": True})
        self.control.side_effect = lambda _data, _worker, action, **_kwargs: (_ for _ in ()).throw(RuntimeError("tray failed")) if action == "desktop_refresh" else None
        with patch("packet_inspector.upgrade.local_json", side_effect=self.health) as api:
            with self.assertRaisesRegex(RuntimeError, "tray failed"):
                self.engine.run()
        switches = [call.args[2] for call in api.call_args_list if call.args[1] == "/__router__/switch"]
        self.assertEqual(switches[-1], {"worker_id": "1" * 32, "expected_active": "2" * 32})
        self.engine._stop_candidate.assert_called_once()

    def test_active_candidate_is_never_stopped_when_rollback_fails(self):
        self.engine.candidate = "2" * 32
        atomic_json(self.data / "router-state.json", {"active": self.engine.candidate})
        self.engine.candidate_process = Mock()
        Upgrade._stop_candidate(self.engine)
        self.control.assert_not_called()
        self.engine.candidate_process.wait.assert_not_called()

    def test_missing_owner_record_never_authorizes_stopping_a_live_entry_worker(self):
        self.engine.candidate = "2" * 32
        self.engine.entry_running = True
        self.engine.candidate_process = Mock()
        self.assertIsNone(self.engine._candidate_active())
        Upgrade._stop_candidate(self.engine)
        self.control.assert_not_called()
        self.engine.candidate_process.wait.assert_not_called()

    def test_lost_native_reply_confirms_the_new_owner_without_rolling_back(self):
        self.engine.candidate = "2" * 32
        descriptor = {"id": self.engine.candidate, "port": 49123, "desktop_tray": True, "presentation_committed": True}
        atomic_json(self.data / "router-workers" / (self.engine.candidate + ".json"), descriptor)
        self.control.side_effect = [None, TimeoutError("reply lost")]
        with patch("packet_inspector.upgrade.healthy_worker", return_value=descriptor), \
                patch("packet_inspector.upgrade.local_json", return_value={"worker_id": self.engine.candidate, "desktop_tray": True}):
            self.engine._refresh_integration({"desktop_refresh_supported": True})

    def test_retired_worker_can_be_restarted_before_rollback(self):
        self.engine.candidate = "2" * 32
        atomic_json(self.data / "router-state.json", {"active": self.engine.candidate})
        atomic_json(self.data / "router-workers" / ("1" * 32 + ".runtime.json"), {"pid": 12345})
        process = Mock()
        process.poll.return_value = None
        with patch("packet_inspector.upgrade.local_json", side_effect=[ValueError("old retired"), {"active": "1" * 32}]), \
                patch("packet_inspector.upgrade.process_alive", return_value=False), \
                patch("packet_inspector.upgrade.start_worker", return_value=process) as start, \
                patch("packet_inspector.upgrade.healthy_worker"):
            self.engine._rollback_switch("1" * 32, "test")
        start.assert_called_once_with(self.data.resolve(), self.config.resolve(), "1" * 32, 47844)

    def test_runtime_version_updates_without_replacing_the_entry_pid(self):
        self.engine.candidate = "2" * 32
        atomic_json(self.data / "runtime.json", {"pid": 12345, "port": 47844, "version": "0.6.7"})
        self.engine._run = Mock(return_value={"state": "complete", "version": "9.0.0"})
        self.engine.run()
        runtime = json.loads((self.data / "runtime.json").read_text(encoding="utf8"))
        self.assertEqual(runtime, {"pid": 12345, "port": 47844, "version": "9.0.0", "worker_id": "2" * 32})

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

    @unittest.skipUnless(os.name == "nt", "Windows file replacement sharing")
    def test_upgrade_result_can_be_replaced_after_an_existing_reader_closes(self):
        path = self.data / "upgrade-result.json"
        atomic_json(path, {"state": "old"})
        reader = path.open("rb")
        timer = threading.Timer(.075, reader.close)
        timer.start()
        try:
            self.engine.journal = {"state": "new"}
            self.engine._save_journal()
        finally:
            reader.close()
            timer.join()
        self.assertEqual(json.loads(path.read_text(encoding="utf8"))["state"], "new")
        self.assertFalse(list(self.data.glob("upgrade-result.json.*.tmp")))

    @unittest.skipUnless(os.name == "nt", "Windows file replacement sharing")
    def test_persistent_replacement_denial_is_bounded_and_other_errors_are_not_retried(self):
        error = PermissionError("replacement denied")
        error.winerror = 5
        with patch("packet_inspector.upgrade.atomic_json", side_effect=error) as write, \
                patch("packet_inspector.upgrade.time.sleep") as wait:
            with self.assertRaises(PermissionError):
                self.engine._save_journal()
            self.assertEqual(write.call_count, 5)
            self.assertAlmostEqual(sum(call.args[0] for call in wait.call_args_list), .5)
        with patch("packet_inspector.upgrade.atomic_json", side_effect=OSError("disk failure")) as write, \
                patch("packet_inspector.upgrade.time.sleep") as wait:
            with self.assertRaises(OSError):
                self.engine._save_journal()
            write.assert_called_once()
            wait.assert_not_called()

    def test_all_upgrade_paths_keep_existing_configuration(self):
        for mode in ("dev", "release"):
            for kind in ("hot", "cold", "legacy"):
                with self.subTest(mode=mode, kind=kind):
                    directory = self.root / (mode + "-" + kind)
                    directory.mkdir()
                    config = directory / "config.toml"
                    config.write_text('# preserved\nmodel="chosen"\nmodel_context_window=280000\n'
                                      'model_auto_compact_token_limit=250000\n', encoding="utf8")
                    data = directory / "data"
                    data.mkdir()
                    for name, value in {
                        "settings.json": {"request_compression": "off", "packet_size_protection_enabled": True,
                                          "image_isolation_enabled": True, "capacity_retry_enabled": True,
                                          "start_on_login": False, "auto_detect_providers": False,
                                          "ui_language": "en", "budget_bytes": 812000, "image_save_dir": "custom"},
                        "updates.json": {"repository": "owner/repo", "automatic": True},
                        "entry-port.json": {"port": 48765},
                        "desktop-profile.json": {"mode": mode, "managed_ui": True},
                        "codex-link.json": {"fixture": "preserve recovery"},
                    }.items():
                        atomic_json(data / name, value)
                    protected = [config, *[data / name for name in ("settings.json", "updates.json", "entry-port.json",
                                                                   "desktop-profile.json", "codex-link.json")]]
                    before = {path: path.read_bytes() for path in protected}
                    atomic_json(data / "router-state.json", {"active": "1" * 32, "token": "fixture-control"})
                    engine = Upgrade(self.payload, data, config, 48765)
                    engine._application_windows = Mock(return_value=[])
                    engine._start_candidate = Mock(side_effect=lambda *_: setattr(engine, "candidate", "2" * 32))
                    engine._start_entry = Mock()
                    engine._wait_entry = Mock(return_value={"worker_id": "2" * 32})

                    def api(_port, path, *_args, **_kwargs):
                        if path == "/health":
                            if kind == "cold":
                                raise ConnectionRefusedError()
                            return {"app": "codex-packet-inspector", **({"router_protocol": 1} if kind == "hot" else {})}
                        if path == "/api/status":
                            return {"data_dir": str(data), "profile": {"config_path": str(config)},
                                    "upgrade_drain_supported": True, "active_requests": 0}
                        return {}

                    with patch("packet_inspector.upgrade.local_json", side_effect=api), \
                            patch("packet_inspector.upgrade.control_worker"), \
                            patch("packet_inspector.upgrade.socket.socket") as socket:
                        socket.return_value.__enter__.return_value.connect_ex.return_value = 1
                        if kind == "legacy":
                            with self.assertRaisesRegex(ValueError, "不中断服务"):
                                engine.run()
                            engine._start_candidate.assert_not_called()
                            engine._start_entry.assert_not_called()
                        else:
                            self.assertEqual(engine.run()["state"], "complete")
                    for path, raw in before.items():
                        self.assertEqual(path.read_bytes(), raw, path.name)


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
