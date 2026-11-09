"""Pending configuration survives upgrades; dismissing it cannot erase a later save."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from packet_inspector.restart_notice import RestartNotices
from tests import test_entry_port as entry_tests


class RestartNoticeBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser contracts")
    def test_persistence_actions_and_bilingual_layout(self):
        result = subprocess.run([shutil.which("node"), "tests/restart_notice.cjs"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, encoding="utf8", timeout=55)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        if "skipped" in summary:
            self.skipTest(summary["skipped"])
        self.assertEqual(summary["restart_notice"], "passed")
        self.assertGreaterEqual(summary["browser_cases"], 8)


class RestartNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.notices = RestartNotices(self.root, self.config)

    def keys(self, notices=None):
        return {row["key"] for row in (notices or self.notices).snapshot()["items"]}

    def test_persist_across_instances_and_keep_only_whitelisted_metadata(self):
        self.notices.mark("connection")
        self.notices.change("context_tokens", None, 250000)
        other = RestartNotices(self.root, self.config)
        self.assertEqual(self.keys(other), {"connection", "context_tokens"})
        stored = json.loads(other.path.read_text(encoding="utf8"))
        self.assertNotIn(str(self.config), json.dumps(stored))
        with self.assertRaises(ValueError):
            other.mark("request_body")
        self.assertEqual(set(other.snapshot()["items"][0]), {"key", "target", "scope"})

    def test_reverting_to_original_value_clears_only_that_setting(self):
        self.notices.change("context_tokens", None, 250000)
        self.notices.change("context_tokens", 250000, 260000)
        self.notices.change("compact_tokens", None, 200000)
        self.notices.change("context_tokens", 260000, None)
        self.assertEqual(self.keys(), {"compact_tokens"})

    def test_later_save_rejects_stale_confirmation_even_for_same_item(self):
        first = self.notices.mark("connection")
        other = RestartNotices(self.root, self.config)
        other.mark("connection")
        with self.assertRaisesRegex(ValueError, "配置又有变更"):
            self.notices.acknowledge("chatgpt", first["revision"])
        self.assertEqual(self.keys(), {"connection"})

    def test_confirm_targets_separately_and_block_unapplied_entry_port(self):
        self.notices.mark("connection")
        self.notices.mark("wsl", target="wsl", scope="Ubuntu")
        first = self.notices.snapshot()
        with self.assertRaisesRegex(ValueError, "请先重启"):
            self.notices.acknowledge("chatgpt", first["revision"], port_pending=True)
        after = self.notices.acknowledge("wsl", first["revision"])
        self.assertEqual(self.keys(), {"connection"})
        self.notices.acknowledge("chatgpt", after["revision"])
        self.assertEqual(self.keys(), set())

    def test_storage_failure_keeps_memory_reminder_and_retries_on_next_save(self):
        with patch.object(self.notices, "_write", side_effect=PermissionError):
            result = self.notices.mark("connection")
            with self.assertRaisesRegex(ValueError, "尚未保存"):
                self.notices.acknowledge("chatgpt", result["revision"])
        self.assertTrue(result["error"])
        self.assertEqual(self.keys(), {"connection"})
        self.notices.change("compact_tokens", None, 200000)
        self.assertFalse(self.notices.snapshot()["error"])
        self.assertEqual(self.keys(RestartNotices(self.root, self.config)), {"connection", "compact_tokens"})

    def test_corrupt_record_is_reported_and_never_silently_overwritten(self):
        self.notices.path.write_text("{broken", encoding="utf8")
        result = self.notices.mark("connection")
        self.assertTrue(result["error"])
        self.assertEqual(self.notices.path.read_text(encoding="utf8"), "{broken")

    def test_acknowledgement_can_retry_a_temporary_write_failure(self):
        with patch.object(self.notices, "_write", side_effect=PermissionError):
            result = self.notices.mark("connection")
        self.notices.acknowledge("chatgpt", result["revision"])
        self.assertEqual(self.keys(RestartNotices(self.root, self.config)), set())

    def test_incomplete_record_is_reported_without_breaking_status(self):
        self.notices.path.write_text(json.dumps({"revision": 1, "items": {"broken": {"key": "connection", "target": "chatgpt"}}}), encoding="utf8")
        self.assertTrue(self.notices.snapshot()["error"])


class RestartNoticeApiTests(unittest.TestCase):
    api = entry_tests.EntryPortTests.api
    stop_server = entry_tests.EntryPortTests.stop_server

    def setUp(self):
        # Network probes have their own suite. Do not let a detached diagnostic
        # writer outlive this reminder-only API fixture and its SQLite files.
        for name, value in (("packet_inspector.wsl_connection.LocalWsl.running", []),
                            ("packet_inspector.server.repair_loopback_proxy", {"changed": False}),
                            ("packet_inspector.network_diagnostics.NetworkDiagnostics.connection", None)):
            mock = patch(name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        entry_tests.EntryPortTests.setUp(self)

    def keys(self, result):
        return {row["key"] for row in result["restart_notice"]["items"]}

    def test_context_acknowledgement_via_desktop_does_not_modify_native_config(self):
        code, result = self.api("/api/context-settings", {"context_tokens": 250000, "compact_tokens": 200000})
        self.assertEqual(code, 200)
        self.assertEqual(self.keys(result), {"context_tokens", "compact_tokens"})
        before = self.config.read_bytes()
        code, notice = self.api("/api/restart-notice", port=self.ui.port)
        self.assertEqual(code, 200)
        code, cleared = self.api("/api/restart-notice", {"target": "chatgpt", "revision": notice["revision"]}, port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertEqual(cleared["items"], [])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.api("/api/status")[1]["restart_notice"]["items"], [])

    def test_failed_notice_storage_does_not_roll_back_saved_context(self):
        with patch.object(self.server.restart_notices, "_write", side_effect=PermissionError):
            code, result = self.api("/api/context-settings", {"context_tokens": 250000, "compact_tokens": None})
        self.assertEqual(code, 200)
        self.assertEqual(result["context_tokens"], 250000)
        self.assertTrue(result["restart_notice"]["error"])

    def test_status_detects_restart_without_acknowledgement_or_config_writes(self):
        self.server.restart_notices.mark("connection")
        self.server.restart_notices.mark("wsl", target="wsl", scope="Ubuntu")
        before = self.config.read_bytes()
        started = time.time_ns() + 1000000000
        with patch("packet_inspector.restart_notice.desktop_started_at_ns", return_value=started), \
             patch("packet_inspector.restart_notice.time.time_ns", return_value=started + 1):
            code, result = self.api("/api/status", port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertEqual(result["restart_notice"]["items"], [{"key": "wsl", "target": "wsl", "scope": "Ubuntu"}])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.api("/api/restart-notice")[1]["items"], result["restart_notice"]["items"])

    def test_port_pending_blocks_acknowledgement_and_is_visible_to_relay(self):
        desired = 47844 if self.server.port != 47844 else 47846
        code, result = self.api("/api/entry-port", {"port": desired}, port=self.ui.port)
        self.assertEqual(code, 200)
        notice = result["restart_notice"]
        self.assertEqual(self.keys(self.api("/api/status")[1]), {"entry_port"})
        code, error = self.api("/api/restart-notice", {"target": "chatgpt", "revision": notice["revision"]}, port=self.ui.port)
        self.assertEqual(code, 400)
        self.assertIn("请先重启", error["error"])

    def test_desktop_can_retry_connection_sync_and_rejects_foreign_origin(self):
        with patch.object(self.server.connection_resume, "retry", return_value={"id": "sync", "state": "pending"}) as retry:
            code, result = self.api("/api/connection/resume", {}, port=self.ui.port)
            self.assertEqual((code, result["state"]), (200, "pending"))
            code, _ = self.api("/api/connection/resume", {}, port=self.ui.port, headers={"Origin": "https://example.invalid"})
            self.assertEqual(code, 403)
            retry.assert_called_once()

    def test_wsl_only_change_does_not_ask_for_chatgpt_restart(self):
        with patch.object(self.server, "sync_providers", return_value={"active": True, "reload_required": False}), \
             patch.object(self.server.wsl, "apply", return_value={"changed": True, "reload_required": True,
                 "targets": [{"distribution": "Ubuntu", "changed": True, "active": True}]}):
            result = self.server.set_connection(True)
        self.assertFalse(result["local_reload_required"])
        self.assertEqual(self.keys(result), {"wsl"})
        self.assertEqual(result["restart_notice"]["items"][0]["target"], "wsl")

    def test_native_retry_and_immediate_settings_are_classified_separately(self):
        result = self.server.set_connection(True)
        self.server.restart_notices.acknowledge("chatgpt", result["restart_notice"]["revision"])
        code, result = self.api("/api/settings", {"capacity_retry_enabled": True, "capacity_retry_max_retries": 4})
        self.assertEqual(code, 200)
        self.assertEqual(self.keys(result), {"capacity_retry"})
        code, result = self.api("/api/settings", {"zstd_level": 4, "max_packets": 100})
        self.assertEqual(code, 200)
        self.assertEqual(self.keys(result), {"capacity_retry"})
        self.api("/api/restart-notice", {"target": "chatgpt", "revision": result["restart_notice"]["revision"]})
        self.server.sync_providers()
        self.assertEqual(self.keys(self.api("/api/status")[1]), set(), "Sticky legacy flags must not recreate an acknowledged notice")
        code, result = self.api("/api/settings", {"capacity_retry_delay_seconds": 51})
        self.assertEqual(code, 200)
        self.assertEqual(self.keys(result), set(), "Live delay settings do not require a native restart")


if __name__ == "__main__":
    unittest.main()
