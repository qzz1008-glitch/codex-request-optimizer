"""Cold startup budgets and bounded diagnostics, without real waits or processes."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from packet_inspector import desktop, router, startup_diagnostics as diagnostics


class Clock:
    def __init__(self):
        self.elapsed = 0

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 1_800_000_000 + self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class StartupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.config = self.data / "config.toml"
        self.config.write_text('model="test"', encoding="utf8")
        self.identity = "a" * 32
        self.state = {"active": self.identity, "draining": [], "port": 47844}
        (self.data / "router-state.json").write_text(json.dumps(self.state), encoding="utf8")
        self.clock = Clock()
        for module in (diagnostics, router, desktop):
            clock = patch.object(module, "time", self.clock)
            clock.start()
            self.addCleanup(clock.stop)
        environment = patch.dict(os.environ, {diagnostics.DEADLINE_ENV: ""})
        environment.start()
        self.addCleanup(environment.stop)

    def records(self):
        return diagnostics.startup_records(diagnostics.log_directory(self.data))

    def entry(self, health, *, process=None):
        process = process or SimpleNamespace(pid=303, poll=lambda: None)
        server = SimpleNamespace(port=47844, state=dict(self.state), state_path=self.data / "router-state.json",
                                 start_monitor=Mock(), serve_forever=Mock(), server_close=Mock())
        with patch.object(router, "RouterServer", return_value=server), \
                patch.object(router, "healthy_worker", side_effect=health), \
                patch.object(router, "start_worker", return_value=process) as start, \
                patch.object(router, "worker_info", return_value={"version": "test"}), \
                patch.object(router, "control_worker"):
            result = router.main(["--data-dir", str(self.data), "--config", str(self.config), "--background"])
        return result, start, server

    def test_entry_accepts_a_cold_worker_after_the_previous_timeout(self):
        def healthy(*_args):
            if self.clock.elapsed < 55:
                raise ConnectionRefusedError(10061, "worker not listening yet")
            return {"version": "test"}
        result, start, server = self.entry(healthy)
        self.assertEqual(result, 0)
        start.assert_called_once()
        server.serve_forever.assert_called_once()
        self.assertGreaterEqual(self.clock.elapsed, 55)
        self.assertLess(self.clock.elapsed, 56)
        self.assertEqual(self.records()[0]["state"], "ready")

    def test_entry_timeout_records_child_phase_and_original_probe_error(self):
        child = None
        def healthy(*_args):
            nonlocal child
            if child is None and self.clock.elapsed > 0:
                child = diagnostics.StartupTrace(self.data, "worker", worker_id=self.identity)
                child.record["pid"] = 303
                child.step("image_tools.initialize")
            raise ConnectionRefusedError(10061, "worker not listening yet")
        with self.assertRaisesRegex(RuntimeError, "worker startup timed out") as failure:
            self.entry(healthy)
        self.assertIsInstance(failure.exception.__cause__, ConnectionRefusedError)
        self.assertGreaterEqual(self.clock.elapsed, 120)
        self.assertLess(self.clock.elapsed, 121)
        record = next(row for row in self.records() if row["role"] == "entry")
        self.assertEqual(record["phase"], "worker.wait")
        self.assertEqual(record["details"]["child_pid"], 303)
        self.assertEqual(record["target_startup"]["phase"], "image_tools.initialize")
        self.assertGreater(record["target_startup"]["phase_elapsed_ms"], 119_000)
        self.assertEqual(record["exception"][1]["errno"], 10061)

    def test_entry_reports_a_worker_exit_without_waiting_for_timeout(self):
        process = SimpleNamespace(pid=303, poll=lambda: 7)
        with self.assertRaisesRegex(RuntimeError, "exit code 7"):
            self.entry(ConnectionRefusedError("not listening"), process=process)
        self.assertEqual(self.clock.elapsed, 0)
        self.assertEqual(self.records()[0]["state"], "failed")

    def test_desktop_waits_for_the_same_cold_start_budget(self):
        options = SimpleNamespace(data_dir=self.data, config=self.config, port=47844, upstream=None)
        status = {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}
        def api(_port, path):
            if self.clock.elapsed < 55:
                raise URLError(ConnectionRefusedError("not listening"))
            return {"app": "codex-packet-inspector"} if path == "/health" else status
        with patch.object(desktop, "local_api", side_effect=api), \
                patch.object(desktop, "start_process", return_value=SimpleNamespace(pid=303, poll=lambda: None)) as start:
            self.assertEqual(desktop.ensure_service(options), status)
        start.assert_called_once()
        self.assertGreaterEqual(self.clock.elapsed, 55)
        self.assertEqual(self.records()[0]["state"], "ready")

    def test_nested_children_share_the_remaining_deadline(self):
        environment = diagnostics.startup_environment()
        with patch.dict(os.environ, environment):
            self.clock.sleep(35)
            self.assertEqual(diagnostics.startup_wait_seconds(), 85)
            self.assertEqual(float(diagnostics.startup_environment()[diagnostics.DEADLINE_ENV]),
                             float(environment[diagnostics.DEADLINE_ENV]))
            self.clock.sleep(90)
            self.assertEqual(diagnostics.startup_wait_seconds(), 0)
            # A later tray action is a new startup, rather than an expired wait.
            fresh = diagnostics.startup_environment()
            self.assertEqual(float(fresh[diagnostics.DEADLINE_ENV]) - self.clock.time(), 120)
        for invalid in ("bad", "0", str(self.clock.time() + 5000)):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {diagnostics.DEADLINE_ENV: invalid}):
                fresh = diagnostics.startup_environment()
                self.assertEqual(float(fresh[diagnostics.DEADLINE_ENV]) - self.clock.time(), 120)

    def test_stage_timings_and_error_chain_are_preserved_and_redacted(self):
        trace = diagnostics.StartupTrace(self.data, "worker")
        trace.step("records.open")
        self.clock.sleep(4)
        trace.step("tls.initialize")
        self.clock.sleep(2)
        trace.fail(URLError(OSError(5, "failed at http://user:private-secret@proxy.invalid password=another-secret")))
        record = self.records()[0]
        self.assertEqual(record["phase"], "tls.initialize")
        self.assertEqual(record["elapsed_ms"], 6000)
        self.assertEqual(record["phase_elapsed_ms"], 2000)
        self.assertEqual(record["stages"][-1], {"phase": "records.open", "elapsed_ms": 4000})
        self.assertEqual(record["exception"][1]["errno"], 5)
        self.assertNotIn("private-secret", trace.path.read_text(encoding="utf8"))
        self.assertNotIn("another-secret", trace.path.read_text(encoding="utf8"))

    def test_snapshots_follow_log_directory_and_have_fixed_disk_bounds(self):
        (self.data / "settings.json").write_text(json.dumps({"log_dir": "diagnostics"}), encoding="utf8")
        for index in range(23):
            trace = diagnostics.StartupTrace(self.data, "entry")
            trace.fail(RuntimeError("x" * 40_000))
            self.clock.sleep(1)
        folder = self.data / "diagnostics" / "startup"
        files = list(folder.glob("*.json"))
        self.assertEqual(len(files), diagnostics.MAX_FILES)
        self.assertTrue(all(p.stat().st_size <= diagnostics.MAX_BYTES for p in files))
        self.assertFalse(list(folder.glob("*.tmp")))
        self.assertEqual(len(self.records()), diagnostics.MAX_FILES)

    def test_corrupt_or_unwritable_diagnostics_do_not_hide_startup_failure(self):
        parent = diagnostics.StartupTrace(self.data, "entry")
        parent.step("worker.wait", worker_id=self.identity, child_pid=303)
        child = diagnostics.StartupTrace(self.data, "worker", worker_id=self.identity)
        child.record.update(pid=303, updated="corrupt", phase_elapsed_ms="corrupt")
        child.path.write_text(json.dumps(child.record), encoding="utf8")
        parent.fail(RuntimeError("original failure"))
        self.assertEqual(parent.record["exception"][0]["message"], "original failure")
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            trace = diagnostics.StartupTrace(self.data, "entry")
            trace.step("worker.wait")
            trace.fail(RuntimeError("still the original failure"))
        self.assertEqual(trace.record["state"], "failed")
