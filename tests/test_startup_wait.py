"""Readiness polling preserves each launcher's failure and cancellation policy."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector import desktop, startup_diagnostics
from packet_inspector.startup_diagnostics import wait_for_startup
from tests.test_startup_diagnostics import Clock


class StartupWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        clock = patch.object(startup_diagnostics, "time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def wait(self, check, seconds=1, **options):
        return wait_for_startup(check, seconds, errors=(ConnectionError,),
                                timeout=lambda: RuntimeError("startup deadline"), **options)

    def test_ready_immediately_including_an_empty_status(self):
        check = Mock(return_value={})
        self.assertEqual(self.wait(check), {})
        check.assert_called_once()
        self.assertEqual(self.clock.elapsed, 0)

    def test_unready_status_and_transient_error_both_keep_the_original_budget(self):
        calls = []
        def check():
            calls.append(round(self.clock.elapsed, 3))
            if len(calls) == 1:
                return None
            if len(calls) == 2:
                raise ConnectionRefusedError("not listening")
            return {"ready": True}
        retry = Mock()
        self.assertEqual(self.wait(check, on_retry=retry), {"ready": True})
        self.assertEqual(calls, [0, .1, .2])
        retry.assert_called_once()

    def test_timeout_retains_the_last_probe_failure_without_extending_the_budget(self):
        error = ConnectionRefusedError("not listening")
        with self.assertRaisesRegex(RuntimeError, "startup deadline") as failed:
            self.wait(Mock(side_effect=error), seconds=.25)
        self.assertIs(failed.exception.__cause__, error)
        self.assertAlmostEqual(self.clock.elapsed, .3)

    def test_zero_budget_does_not_probe_or_launch_anything(self):
        check, retry = Mock(), Mock()
        with self.assertRaises(RuntimeError):
            self.wait(check, seconds=0, on_retry=retry)
        check.assert_not_called()
        retry.assert_not_called()

    def test_identity_validation_errors_are_never_retried(self):
        retry = Mock()
        with self.assertRaisesRegex(ValueError, "different profile"):
            self.wait(Mock(side_effect=ValueError("different profile")), on_retry=retry)
        retry.assert_not_called()
        self.assertEqual(self.clock.elapsed, 0)

    def test_exited_worker_is_checked_before_accepting_a_health_reply(self):
        check = Mock(return_value={"ready": True})
        with self.assertRaisesRegex(RuntimeError, "worker exited"):
            self.wait(check, before=Mock(side_effect=RuntimeError("worker exited")))
        check.assert_not_called()

    def test_monitor_can_cancel_before_any_probe(self):
        check = Mock()
        self.assertIsNone(self.wait(check, wait=Mock(return_value=True), wait_first=True))
        check.assert_not_called()

    def test_monitor_can_cancel_after_a_failed_probe(self):
        check = Mock(side_effect=ConnectionRefusedError())
        self.assertIsNone(self.wait(check, wait=Mock(return_value=True)))
        check.assert_called_once()

    def test_upgrade_timeout_keeps_its_existing_exception_chaining_policy(self):
        with self.assertRaises(RuntimeError) as failed:
            self.wait(Mock(side_effect=ConnectionRefusedError()), seconds=.1, chain_error=False)
        self.assertIsNone(failed.exception.__cause__)

    def test_concurrent_desktop_launcher_may_exit_before_another_service_is_ready(self):
        options = SimpleNamespace(port=47844, data_dir=Path("synthetic-data").resolve(),
                                  config=Path("synthetic-config").resolve(), upstream=None)
        status = {"data_dir": str(options.data_dir), "profile": {"config_path": str(options.config)}}
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                self.clock.elapsed = 0
                def api(_port, path):
                    if self.clock.elapsed < .4:
                        raise ConnectionRefusedError()
                    return {"app": "codex-packet-inspector"} if path == "/health" else status
                trace = SimpleNamespace(record={"details": {}}, step=Mock(), ready=Mock())
                with patch.object(desktop, "time", self.clock), \
                        patch.object(desktop, "startup_wait_seconds", return_value=1), \
                        patch.object(desktop, "local_api", side_effect=api), \
                        patch.object(desktop, "start_process", return_value=SimpleNamespace(pid=303, poll=lambda: exit_code)) as spawn:
                    self.assertEqual(desktop._ensure_service(options, trace), status)
                spawn.assert_called_once()
                trace.ready.assert_called_once()
