import http.client
import json
from pathlib import Path
import unittest
import tempfile
import sqlite3
from unittest.mock import patch

from tests import test_packet_inspector as fixtures
from tests.test_capacity_retry import CREATED, FAILED, SUCCESS, Response, frame
from packet_inspector.startup import configure_startup, startup_supported
from packet_inspector.server import InspectorServer


class RuntimeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    api = fixtures.RelayTests.api
    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest

    def setUp(self):
        retry_delay = patch("packet_inspector.capacity_retry.retry_delay", return_value=0)
        retry_delay.start()
        self.addCleanup(retry_delay.stop)
        self.server.save_settings({**self.server.settings, "start_on_login": False, "capacity_retry_enabled": False,
                                   "capacity_retry_max_retries": 3})
        self.server.startup_state_known = True

    def test_defaults_and_capabilities(self):
        status, result = self.api("/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(result["start_on_login"])
        self.assertFalse(result["capacity_retry_enabled"])
        self.assertEqual(result["capacity_retry_max_retries"], 3)
        self.assertFalse(result["start_on_login_supported"])
        self.assertTrue(result["capacity_retry_supported"])
        self.assertTrue(result["capacity_retry_diagnostics_supported"])

    def test_capacity_setting_is_persisted_and_can_be_disabled(self):
        for enabled in [True, False]:
            status, result = self.api("/api/settings", {"capacity_retry_enabled": enabled})
            self.assertEqual(status, 200)
            self.assertEqual(result["capacity_retry_enabled"], enabled)
            saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
            self.assertEqual(saved["capacity_retry_enabled"], enabled)

    def test_language_is_persisted_and_invalid_changes_are_atomic(self):
        try:
            for language in ("en", "zh"):
                status, result = self.api("/api/settings", {"ui_language": language})
                self.assertEqual((status, result["ui_language"]), (200, language))
                self.assertEqual(json.loads(self.server.settings_path.read_text())["ui_language"], language)
            before = self.server.settings_path.read_bytes()
            for invalid in (None, [], True, "fr"):
                self.assertEqual(self.api("/api/settings", {"ui_language": invalid, "capacity_retry_enabled": True})[0], 400)
                self.assertEqual(self.server.settings_path.read_bytes(), before)
        finally:
            self.server.save_settings({**self.server.settings, "ui_language": "zh"})

    def test_boolean_validation_does_not_accept_numbers_or_strings(self):
        for key in ["start_on_login", "capacity_retry_enabled"]:
            for value in [1, "true", None]:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.api("/api/settings", {key: value})[0], 400)
                    self.assertFalse(self.server.settings[key])

    def test_retry_limit_is_persisted(self):
        for limit in [1, 5, 9]:
            with self.subTest(limit=limit):
                status, result = self.api("/api/settings", {"capacity_retry_max_retries": limit})
                self.assertEqual(status, 200)
                self.assertEqual(result["capacity_retry_max_retries"], limit)
                saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
                self.assertEqual(saved["capacity_retry_max_retries"], limit)

    def test_invalid_retry_limit_rejects_entire_settings_change(self):
        before = self.server.settings_path.read_bytes()
        for limit in [0, -1, 10, 99, 100, True, False, "3", 2.5, None]:
            with self.subTest(limit=limit):
                status, _ = self.api("/api/settings", {"capacity_retry_enabled": True,
                                                      "capacity_retry_max_retries": limit})
                self.assertEqual(status, 400)
                self.assertFalse(self.server.settings["capacity_retry_enabled"])
                self.assertEqual(self.server.settings["capacity_retry_max_retries"], 3)
                self.assertEqual(self.server.settings_path.read_bytes(), before)

    def test_loading_previous_retry_limits_preserves_other_settings(self):
        for limit, expected in [(3, 3), (9, 9), (12, 9), (99, 9)]:
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = root / "config.toml"
                config.write_text("", encoding="utf8")
                data = root / "data"
                data.mkdir()
                saved = {"capacity_retry_enabled": True, "capacity_retry_max_retries": limit,
                         "zstd_level": 11, "packet_size_protection_enabled": True}
                (data / "settings.json").write_text(json.dumps(saved), encoding="utf8")
                with patch("packet_inspector.server.startup_supported", return_value=False):
                    server = InspectorServer(("127.0.0.1", 0), data, config, "http://127.0.0.1:9/v1")
                try:
                    self.assertEqual(server.settings["capacity_retry_max_retries"], expected)
                    self.assertTrue(server.settings["capacity_retry_enabled"])
                    self.assertEqual(server.settings["zstd_level"], 11)
                    self.assertTrue(server.settings["packet_size_protection_enabled"])
                    self.assertEqual(json.loads(server.settings_path.read_text())["capacity_retry_max_retries"], expected)
                finally:
                    server.server_close()

    def test_login_setting_uses_installation_helper_without_stopping_service(self):
        with patch("packet_inspector.server.configure_startup") as configure:
            for enabled in [True, False]:
                status, result = self.api("/api/settings", {"start_on_login": enabled})
                self.assertEqual(status, 200)
                self.assertEqual(result["start_on_login"], enabled)
                configure.assert_called_with(enabled, self.server.data_dir, self.server.link.config, self.server.port)
                self.assertEqual(self.api("/health")[0], 200)
        self.assertEqual(configure.call_count, 2)

    def test_failed_startup_change_rolls_back_combined_settings(self):
        before = self.server.settings_path.read_bytes()
        with patch("packet_inspector.server.configure_startup", side_effect=ValueError("Task registration failed")):
            status, _ = self.api("/api/settings", {"start_on_login": True, "capacity_retry_enabled": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.server.settings_path.read_bytes(), before)
        self.assertFalse(self.server.settings["start_on_login"])
        self.assertFalse(self.server.settings["capacity_retry_enabled"])

    def test_unknown_startup_state_can_be_reconciled_without_changing_checkbox(self):
        self.server.startup_state_known = False
        with patch("packet_inspector.server.configure_startup") as configure:
            status, result = self.api("/api/settings", {"start_on_login": False})
        self.assertEqual(status, 200)
        configure.assert_called_once()
        self.assertTrue(result["startup_state_known"])

    def test_disposable_instance_cannot_modify_login_task(self):
        self.assertEqual(self.api("/api/settings", {"start_on_login": True})[0], 400)
        self.assertFalse(self.server.settings["start_on_login"])

    def test_enabled_retry_replays_same_request_and_records_retry(self):
        self.api("/api/settings", {"capacity_retry_enabled": True})
        first = Response(CREATED + FAILED, extra={"Retry-After": "0"})
        second = Response(SUCCESS)
        raw = b'{"model":"original","stream":true,"input":[]}'
        with patch.object(self.server.local_opener, "open", side_effect=[first, second]) as opened:
            status, data = self.relay(raw)
        self.assertEqual((status, data), (200, SUCCESS))
        self.assertEqual(opened.call_count, 2)
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertIs(requests[0], requests[1])
        self.assertEqual(requests[0].data, raw)
        self.assertEqual(requests[0].get_header("Accept-encoding"), "identity")
        metadata, _ = self.latest()
        self.assertEqual(metadata["capacity_retry_count"], 1)
        self.assertEqual(metadata["state"], "complete")
        diagnostic = metadata["capacity_retry_diagnostic"]
        self.assertEqual((diagnostic["outcome"], diagnostic["state"], diagnostic["attempt_count"]), ("success", "finished", 2))
        self.assertTrue(diagnostic["journal_saved"])
        self.assertTrue(diagnostic["enabled"])
        self.assertEqual(self.api("/api/packets/" + metadata["id"])[1]["capacity_retry_diagnostic"]["outcome"], "success")
        events = self.api("/api/retry-diagnostics/" + diagnostic["id"])[1]["events"]
        starts = [index for index, event in enumerate(events) if event["event"] == "attempt_started"]
        headers = next(index for index, event in enumerate(events) if event["event"] == "downstream_headers_sent")
        forwarded = next(index for index, event in enumerate(events) if event["event"] == "downstream_first_bytes_sent")
        self.assertLess(starts[1], headers)
        self.assertLess(headers, forwarded)
        self.assertEqual(sum(event["event"] == "downstream_headers_sent" for event in events), 1)

    def test_disabled_retry_passes_failure_unchanged(self):
        with patch.object(self.server.local_opener, "open", return_value=Response(CREATED + FAILED)) as opened:
            status, data = self.relay(b"{}")
        self.assertEqual((status, data, opened.call_count), (200, CREATED + FAILED, 1))
        diagnostic = self.latest()[0]["capacity_retry_diagnostic"]
        self.assertEqual((diagnostic["outcome"], diagnostic["stop_reason"], diagnostic["retry_count"]), ("capacity_error", "disabled", 0))
        self.assertFalse(diagnostic["enabled"])

    def test_relay_uses_configured_retry_limit(self):
        for limit in [1, 5]:
            with self.subTest(limit=limit):
                self.assertEqual(self.api("/api/settings", {"capacity_retry_enabled": True,
                                                           "capacity_retry_max_retries": limit})[0], 200)
                responses = [Response(FAILED, extra={"Retry-After": "0"}) for _ in range(limit + 1)]
                with patch.object(self.server.local_opener, "open", side_effect=responses) as opened:
                    status, data = self.relay(b"{}")
                self.assertEqual((status, data, opened.call_count), (200, FAILED, limit + 1))
                metadata, _ = self.latest()
                self.assertEqual(metadata["capacity_retry_count"], limit)
                diagnostic = metadata["capacity_retry_diagnostic"]
                self.assertEqual((diagnostic["outcome"], diagnostic["stop_reason"]), ("capacity_error", "retry_limit"))
                record = self.api("/api/retry-diagnostics/" + diagnostic["id"])[1]
                self.assertEqual(sum(e["event"] == "attempt_started" for e in record["events"]), limit + 1)

    def test_capacity_after_empty_output_is_observed_without_replay(self):
        self.api("/api/settings", {"capacity_retry_enabled": True})
        body = CREATED + frame("response.output_item.added", item={"type": "reasoning", "summary": []}) + FAILED
        with patch.object(self.server.local_opener, "open", return_value=Response(body)) as opened:
            self.assertEqual(self.relay(b"{}"), (200, body))
        self.assertEqual(opened.call_count, 1)
        diagnostic = self.latest()[0]["capacity_retry_diagnostic"]
        self.assertEqual((diagnostic["outcome"], diagnostic["stop_reason"], diagnostic["retry_count"]),
                         ("capacity_error", "stream_committed", 0))
        self.assertEqual(diagnostic["first_output_event"], "response.output_item.added")
        events = self.api("/api/retry-diagnostics/" + diagnostic["id"])[1]["events"]
        probe = next(e for e in events if e["event"] == "probe_finished")
        self.assertEqual((probe["item_type"], probe["summary_items"], probe["content_items"]), ("reasoning", 0, 0))

    def test_retry_journal_query_download_and_packet_eviction(self):
        with patch.object(self.server.local_opener, "open", return_value=Response(FAILED)):
            self.relay(b"{}")
        metadata = self.latest()[0]
        request_id = metadata["capacity_retry_diagnostic"]["id"]
        self.assertTrue(self.api("/api/packets/" + metadata["id"])[1]["capacity_retry_diagnostic"]["journal_available"])
        # Match normal retention: removing packet+summary must not remove the audit.
        with self.server.store.lock, self.server.store.connect() as db:
            db.execute("DELETE FROM packets WHERE id=?", (metadata["id"],))
        self.assertEqual(self.api("/api/packets/" + metadata["id"])[0], 404)
        status, result = self.api("/api/retry-diagnostics?session_id=test-thread&packet_id=" + request_id)
        self.assertEqual((status, len(result["records"])), (200, 1))
        self.assertNotIn("events", result["records"][0])
        self.assertEqual(result["records"][0]["outcome"], "capacity_error")
        self.assertEqual(self.api("/api/retry-diagnostics?limit=101")[0], 400)
        self.assertEqual(self.api("/api/retry-diagnostics/missing")[0], 404)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            connection.request("GET", "/api/retry-diagnostics/" + request_id + "?download=1")
            response = connection.getresponse()
            record = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertIn("attachment", response.getheader("Content-Disposition"))
            self.assertEqual(record["session_id"], "test-thread")
            self.assertEqual(record["outcome"], "capacity_error")
        finally:
            connection.close()

    def test_pruned_audit_does_not_offer_broken_download(self):
        with patch.object(self.server.local_opener, "open", return_value=Response(SUCCESS)):
            self.relay(b"{}")
        metadata = self.latest()[0]
        with self.server.retry_journal.lock, self.server.retry_journal._connect() as db:
            db.execute("DELETE FROM retry_requests WHERE id=?", (metadata["id"],))
        diagnostic = self.api("/api/packets/" + metadata["id"])[1]["capacity_retry_diagnostic"]
        self.assertFalse(diagnostic["journal_available"])
        self.assertEqual(diagnostic["outcome"], "success")

    def test_paused_capture_still_audits_and_native_compact_is_not_retried(self):
        self.api("/api/settings", {"capacity_retry_enabled": True, "capture_enabled": False})
        try:
            with patch.object(self.server.local_opener, "open", return_value=Response(FAILED)) as opened:
                self.assertEqual(self.relay(b"{}", path="/responses/compact", headers={"session_id": "compact-audit"}), (200, FAILED))
            self.assertEqual(opened.call_count, 1)
            records = self.api("/api/retry-diagnostics?session_id=compact-audit")[1]["records"]
            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0]["packet_id"])
            self.assertEqual((records[0]["outcome"], records[0]["stop_reason"]), ("capacity_error", "endpoint_not_eligible"))
            self.assertFalse(records[0]["eligible"])
        finally:
            self.api("/api/settings", {"capture_enabled": True})

    def test_audit_failure_does_not_interrupt_forwarding(self):
        for target in ("begin", "save"):
            with self.subTest(target=target), patch.object(self.server.retry_journal, target, side_effect=sqlite3.OperationalError("private details")), \
                    patch.object(self.server.local_opener, "open", return_value=Response(SUCCESS)):
                self.assertEqual(self.relay(b"{}"), (200, SUCCESS))
            self.assertEqual(self.latest()[0]["state"], "complete")
        self.assertGreater(self.api("/api/status")[1]["capacity_retry_diagnostics_errors"], 0)


class StartupHelperTests(unittest.TestCase):
    def test_enable_and_disable_use_specific_task_actions(self):
        folder = Path("disposable")
        with patch("packet_inspector.startup.startup_supported", return_value=True), \
                patch("packet_inspector.startup.manage_installation", return_value={"supported": True}) as manage:
            configure_startup(True, folder, folder / "config.toml", 1234)
            self.assertEqual(manage.call_args.args[0], "EnableStartup")
            configure_startup(False, folder, folder / "config.toml", 1234)
            self.assertEqual(manage.call_args.args[0], "DisableStartup")

    def test_source_or_disposable_directories_cannot_configure_startup(self):
        self.assertFalse(startup_supported(Path("disposable")))
        with self.assertRaisesRegex(ValueError, "Windows"):
            configure_startup(True, Path("disposable"), Path("config.toml"), 1234)


class StartupInitializationTests(unittest.TestCase):
    def test_existing_login_state_is_read_without_changing_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            with patch("packet_inspector.server.startup_supported", return_value=True), \
                    patch("packet_inspector.server.manage_installation", return_value={"login_start": True}) as manage:
                server = InspectorServer(("127.0.0.1", 0), root / "data", config, "http://127.0.0.1:9/v1")
                try:
                    self.assertTrue(server.settings["start_on_login"])
                    self.assertTrue(server.startup_state_known)
                    self.assertEqual(manage.call_args.args[0], "Status")
                finally:
                    server.server_close()

    def test_unavailable_task_scheduler_does_not_prevent_service_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            with patch("packet_inspector.server.startup_supported", return_value=True), \
                    patch("packet_inspector.server.manage_installation", side_effect=ValueError("Unavailable")):
                server = InspectorServer(("127.0.0.1", 0), root / "data", config, "http://127.0.0.1:9/v1")
                try:
                    self.assertFalse(server.startup_state_known)
                    self.assertFalse(server.settings["start_on_login"])
                    self.assertEqual(server.status()["storage"]["count"], 0)
                finally:
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
