"""A restart reminder is cleared only by a desktop lifetime after the save."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.desktop_restart import desktop_started_at_ns
from packet_inspector.restart_notice import RestartNotices


class DesktopRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n', encoding="utf8")
        os.utime(self.config, ns=(100, 100))
        self.notices = RestartNotices(self.root, self.config)
        self.probe = self.enterContext(patch("packet_inspector.restart_notice.desktop_started_at_ns", return_value=None))
        self.clock = self.enterContext(patch("packet_inspector.restart_notice.time.time_ns", return_value=1000))
        self.elapsed = self.enterContext(patch("packet_inspector.restart_notice.time.monotonic", return_value=0))

    def snapshot(self, *, notices=None, port_pending=False):
        self.elapsed.return_value += 3
        return (notices or self.notices).snapshot(port_pending=port_pending)

    def restarted(self, started=2000):
        self.probe.return_value = started
        self.clock.return_value = started + 1000

    def test_existing_desktop_absent_desktop_and_unknown_state_keep_reminder(self):
        self.notices.mark("connection")
        for started in (None, 500, 1000):
            with self.subTest(started=started):
                self.probe.return_value = started
                self.assertEqual(len(self.snapshot()["items"]), 1)

    def test_restarted_desktop_clears_only_chatgpt_and_persists_across_services(self):
        self.notices.change("image_analysis_mode", "native", "optimized")
        self.notices.mark("wsl", target="wsl", scope="Ubuntu")
        self.restarted()
        other = RestartNotices(self.root, self.config)
        result = self.snapshot(notices=other)
        self.assertEqual(result["items"], [{"key": "wsl", "target": "wsl", "scope": "Ubuntu"}])
        self.assertEqual(self.notices.snapshot(), result)

    def test_new_save_after_restart_and_external_config_changes_keep_reminder(self):
        self.notices.mark("connection")
        self.restarted()
        os.utime(self.config, ns=(2500, 2500))
        self.assertEqual(len(self.snapshot()["items"]), 1)
        self.restarted(4000)
        self.assertFalse(self.snapshot()["items"])
        self.notices.change("context_tokens", None, 250000)
        self.assertEqual(len(self.snapshot()["items"]), 1)

    def test_concurrent_save_during_detection_is_not_acknowledged(self):
        self.notices.mark("connection")
        self.restarted()

        def concurrent_save():
            RestartNotices(self.root, self.config).change("compact_tokens", None, 200000)
            return 2000

        self.probe.side_effect = concurrent_save
        self.assertTrue(self.snapshot()["items"])
        self.assertEqual({item["key"] for item in self.notices.snapshot()["items"]}, {"connection", "compact_tokens"})

    def test_pending_or_invalid_port_blocks_detection_until_optimizer_applies_it(self):
        self.notices.change("entry_port", 47844, 47848)
        self.restarted()
        self.assertEqual(len(self.snapshot(port_pending=True)["items"]), 1)
        self.probe.assert_not_called()
        # The optimizer applies the new provider URL after this desktop start.
        os.utime(self.config, ns=(2500, 2500))
        self.assertEqual(len(self.snapshot()["items"]), 1)
        self.restarted(4000)
        self.assertFalse(self.snapshot()["items"])

    def test_wsl_confirmation_does_not_hide_an_observed_chatgpt_restart(self):
        self.notices.mark("connection")
        result = self.notices.mark("wsl", target="wsl", scope="Ubuntu")
        self.restarted()
        self.notices.acknowledge("wsl", result["revision"])
        self.assertFalse(self.snapshot()["items"])

    def test_legacy_and_older_writer_records_use_the_last_write_boundary(self):
        self.notices.mark("connection")
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                data = json.loads(self.notices.path.read_text(encoding="utf8"))
                if legacy:
                    data.pop("desktop_restart", None)
                else:
                    data["desktop_restart"] = {"revision": data["revision"] - 1, "changed_at_ns": 1000}
                self.notices.path.write_text(json.dumps(data), encoding="utf8")
                os.utime(self.notices.path, ns=(2500, 2500))
                self.restarted()
                self.assertEqual(len(self.snapshot()["items"]), 1)
        self.restarted(4000)
        self.assertFalse(self.snapshot()["items"])

    def test_detection_is_throttled_and_never_runs_for_wsl_only(self):
        self.notices.mark("wsl", target="wsl", scope="Ubuntu")
        self.snapshot()
        self.probe.assert_not_called()
        self.notices.mark("connection")
        self.notices.snapshot(port_pending=False)
        self.notices.snapshot(port_pending=False)
        self.assertEqual(self.probe.call_count, 1)
        self.snapshot()
        self.assertEqual(self.probe.call_count, 2)

    def test_failed_write_missing_config_and_future_creation_time_keep_reminder(self):
        self.notices.mark("connection")
        self.restarted()
        with patch.object(self.notices, "_write", side_effect=PermissionError):
            self.assertEqual(len(self.snapshot()["items"]), 1)
        self.probe.return_value = 4000
        self.assertEqual(len(self.snapshot()["items"]), 1)
        self.config.unlink()
        self.assertEqual(len(self.snapshot()["items"]), 1)
        result = self.notices.snapshot()
        self.notices.acknowledge("chatgpt", result["revision"])
        self.assertFalse(self.notices.snapshot()["items"])


class DesktopProbeTests(unittest.TestCase):
    def test_unsupported_platform_and_access_failure_are_inconclusive(self):
        with patch("packet_inspector.desktop_restart.os.name", "posix"):
            self.assertIsNone(desktop_started_at_ns())
        with patch("packet_inspector.desktop_restart.os.name", "nt"), patch(
                "packet_inspector.desktop_restart._windows_started_at_ns", side_effect=PermissionError):
            self.assertIsNone(desktop_started_at_ns())


if __name__ == "__main__":
    unittest.main()
