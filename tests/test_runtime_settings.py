import http.client
import json
from pathlib import Path
import unittest
import tempfile
import sqlite3
import time
from unittest.mock import patch

from tests import test_packet_inspector as fixtures
from tests.response_fixtures import CREATED, FAILED, SUCCESS, Response, frame
from packet_inspector.startup import configure_startup, startup_supported
from packet_inspector.server import InspectorServer
from packet_inspector.analyzer import encode, inspect_request


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
        self.server.save_settings({**self.server.settings, "start_on_login": False, "capacity_retry_enabled": False,
                                   "capacity_retry_max_retries": 3, "capacity_retry_delay_seconds": 50})
        self.server.startup_state_known = True

    def test_exact_storage_byte_limits_survive_unrelated_settings_updates(self):
        previous = dict(self.server.settings)
        def restore():
            self.server.save_settings(previous)
            self.server.store.configure_limits(previous["max_storage_bytes"], previous["max_packets"], previous["max_detail_packets"])
        self.addCleanup(restore)
        status, result = self.api("/api/settings", {"max_storage_bytes": 512000123})
        self.assertEqual(status, 200)
        self.assertEqual(result["max_storage_bytes"], 512000123)
        self.assertEqual(result["storage"]["max_bytes"], 512000123)
        self.assertEqual(result["max_storage_mb"], 489)
        status, result = self.api("/api/settings", {"capture_enabled": previous["capture_enabled"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["storage"]["max_bytes"], 512000123)
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        self.assertEqual(saved["max_storage_bytes"], 512000123)

    def test_invalid_storage_byte_limits_reject_the_entire_update(self):
        previous = dict(self.server.settings)
        for fields in ({"max_storage_bytes": value} for value in (True, "512000000", 512000000.0, 0, 8589934593)):
            with self.subTest(fields=fields):
                status, _ = self.api("/api/settings", {"max_packets": 10, **fields})
                self.assertEqual(status, 400)
                self.assertEqual(self.server.settings, previous)
        status, _ = self.api("/api/settings", {"max_storage_bytes": 512000000, "max_storage_mb": 512})
        self.assertEqual(status, 400)
        self.assertEqual(self.server.settings, previous)

    def test_defaults_and_capabilities(self):
        status, result = self.api("/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(result["start_on_login"])
        self.assertFalse(result["capacity_retry_enabled"])
        self.assertEqual(result["capacity_retry_max_retries"], 3)
        self.assertFalse(result["start_on_login_supported"])
        self.assertTrue(result["capacity_retry_supported"])
        self.assertTrue(result["capacity_retry_diagnostics_supported"])
        self.assertIsInstance(result["max_detail_packets"], int)
        self.assertLessEqual(result["max_detail_packets"], result["max_packets"])
        oldest = self.server.store.list(limit=1, sort="created", direction="asc")
        self.assertEqual(result["storage"]["oldest_created"], oldest[0]["created"] if oldest else None)

    def test_tray_status_returns_aggregate_counters_without_request_payloads(self):
        statistics = {"recorded_requests": 20, "protected_requests": 3,
                      "compaction_samples": 2, "average_compact_input_tokens": 200000}
        with patch.object(self.server, "connection_profile", return_value={"active": True}), \
                patch.object(self.server.store, "tray_statistics", return_value=statistics):
            status, result = self.api("/api/tray-status")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"connection_state": "connected", "relay_state": "running",
                                  "language": self.server.settings["ui_language"], **statistics})

    def test_trend_projection_keeps_real_usage_and_markers_without_response_payloads(self):
        now = time.time()
        meta = {"thread_id": "trend-api-fixture", "method": "POST", "path": "/responses",
                "upstream_usage": {"input_tokens": 12345, "output_tokens": 30, "payload": "x" * 200000},
                "reported_usage": {"input_tokens": 500000}, "input_items": [{"kind": "message:user", "label": "private"}],
                "capacity_retry_diagnostic": {"events": ["private"]}, "outbound_wire_bytes": 800000,
                "usage_override_applied": True, "overflow_cache_hit": True, "native_retry_advised": True}
        for age in (3600, 13 * 3600):
            with patch("packet_inspector.storage.time.time", return_value=now - age):
                packet = self.server.store.add(meta)
            self.server.store.update(packet, state="complete", status=200)
        status, compact = self.api(f"/api/packets?thread=trend-api-fixture&view=trend&since={now - 12 * 3600}")
        self.assertEqual(status, 200)
        self.assertEqual(len(compact), 1)
        row = compact[0]
        self.assertEqual(row["upstream_usage"], {"input_tokens": 12345})
        self.assertEqual(row["outbound_wire_bytes"], 800000)
        for key in ("usage_override_applied", "overflow_cache_hit", "native_retry_advised"):
            self.assertIs(row[key], True)
        for key in ("reported_usage", "input_items", "capacity_retry_diagnostic"):
            self.assertNotIn(key, row)
        self.assertLess(len(json.dumps(compact)), 2000)
        self.assertEqual(self.api("/api/packets/" + row["id"] + "?view=trend"), (200, row))
        status, full = self.api("/api/packets?thread=trend-api-fixture")
        self.assertEqual(status, 200)
        self.assertEqual(len(full), 2)
        self.assertEqual(full[0]["upstream_usage"], meta["upstream_usage"])
        self.assertGreater(len(json.dumps(full)), 400000)
        self.assertTrue(self.api("/api/status")[1]["trend_packets_supported"])

    def test_trend_projection_never_substitutes_reported_or_missing_usage(self):
        meta = {"thread_id": "trend-missing-fixture", "method": "POST", "path": "/responses",
                "reported_usage": {"input_tokens": 500000}, "usage_override_applied": True,
                "request_purpose": "image_analysis", "is_compaction": True}
        packet = self.server.store.add(meta)
        self.server.store.update(packet, state="complete", status=200)
        status, rows = self.api("/api/packets?thread=trend-missing-fixture&view=trend")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("upstream_usage", rows[0])
        self.assertNotIn("reported_usage", rows[0])
        self.assertEqual(rows[0]["request_purpose"], "image_analysis")
        self.assertTrue(rows[0]["is_compaction"])

    def test_automatic_details_use_the_storage_budget_and_keep_basic_information(self):
        previous = dict(self.server.settings)
        def restore():
            self.server.save_settings(previous)
            self.server.store.configure_limits(previous["max_storage_mb"] * 1024 * 1024,
                                               previous["max_packets"], previous["max_detail_packets"])
        self.addCleanup(restore)
        status, result = self.api("/api/settings", {"max_packets": 10, "max_detail_packets": 1, "max_storage_mb": 16})
        self.assertEqual(status, 200)
        self.assertEqual((result["max_packets"], result["max_detail_packets"], result["max_storage_mb"]), (10, 10, 16))
        self.assertEqual(result["retention_policy"], "records_first")
        self.assertIsNone(self.server.store.max_detail_count)
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        self.assertEqual(saved["max_detail_packets"], 10)
        packets = []
        for index in range(10):
            meta, content = inspect_request(encode({"model": "test", "input": [{"role": "user", "content": "private body"}]}),
                                            {"session_id": "retention-api"}, "/responses")
            meta.update(method="POST", path="/responses")
            packet = self.server.store.add(meta, content)
            self.server.store.update(packet, state="complete", status=200,
                                     upstream_usage={"input_tokens": 456, "output_tokens": 12, "total_tokens": 468})
            packets.append(packet)
        self.assertTrue(all(self.server.store.get(packet, content=True)[1] is not None for packet in packets))
        self.assertEqual(self.server.store.stats()["count"], 10)
        budget = self.server.store.stats()["stored"] - 1
        self.server.store.configure_limits(budget, 10)
        status, meta = self.api("/api/packets/" + packets[0])
        self.assertEqual(status, 200)
        self.assertTrue(meta["details_evicted"])
        self.assertIn("top_fields", meta)
        self.assertIn("input_groups", meta)
        self.assertNotIn("input_items", meta)
        status, export = self.api("/api/packets/" + packets[0] + "/export")
        self.assertEqual(status, 200)
        self.assertIsNone(export["view"])
        self.assertNotIn("private body", json.dumps(export))
        status, usage = self.api("/api/packets/" + packets[0] + "/token-window")
        self.assertEqual(status, 200)
        self.assertEqual(usage["input_tokens"], 456)
        status, diff = self.api("/api/packets/" + packets[1] + "/diff")
        self.assertEqual(status, 200)
        self.assertEqual(diff["state"], "summary_only")
        self.assertEqual(diff["previous_id"], packets[0])
        self.assertEqual(diff["decoded_delta"], 0)
        self.assertEqual(diff["fields"], [])
        self.assertNotIn("added", diff)
        status, diff = self.api("/api/packets/" + packets[-1] + "/diff")
        self.assertEqual(status, 200)
        self.assertEqual(diff["previous_id"], packets[-2])
        self.assertNotEqual(diff.get("state"), "details_unavailable")
        status, export = self.api("/api/packets/" + packets[-1] + "/export")
        self.assertEqual(status, 200)
        self.assertIsNotNone(export["view"])
        status, result = self.api("/api/settings", {"max_detail_packets": 0})
        self.assertEqual(status, 200)
        self.assertEqual(result["max_detail_packets"], 10)
        self.assertGreater(result["storage"]["detail_count"], 0)
        self.assertLessEqual(result["storage"]["stored"], result["storage"]["max_bytes"])

    def test_legacy_zero_detail_limit_keeps_full_capture_and_forwarding(self):
        before = dict(self.server.settings)
        def restore():
            self.server.save_settings(before)
            self.server.store.configure_limits(before["max_storage_mb"] * 1024 * 1024,
                                               before["max_packets"], before["max_detail_packets"])
        self.addCleanup(restore)
        status, _ = self.api("/api/settings", {"max_detail_packets": 0})
        self.assertEqual(status, 200)
        raw = b'{"model":"test","input":[{"role":"user","content":"complete private request body"}]}'
        status, response = self.relay(raw)
        self.assertEqual(status, 200)
        self.assertTrue(response)
        self.assertEqual(self.up.requests[-1]["body"], raw)
        packet = self.server.store.list(limit=1)[0]
        meta, content = self.server.store.get(packet["id"], content=True)
        self.assertIsNotNone(content)
        self.assertFalse(meta.get("details_evicted", False))
        self.assertEqual(meta["wire_bytes"], len(raw))

    def test_invalid_detail_limits_do_not_partially_save_settings(self):
        before = dict(self.server.settings)
        for value in (-1, True, 1.5, 20001, before["max_packets"] + 1):
            with self.subTest(value=value):
                status, _ = self.api("/api/settings", {"max_detail_packets": value, "max_storage_mb": 32})
                self.assertEqual(status, 400)
                self.assertEqual(self.server.settings, before)

    def test_record_limit_changes_always_release_the_legacy_detail_cap(self):
        before = dict(self.server.settings)
        def restore():
            self.server.save_settings(before)
            self.server.store.configure_limits(before["max_storage_mb"] * 1024 * 1024,
                                               before["max_packets"], before["max_detail_packets"])
        self.addCleanup(restore)
        status, result = self.api("/api/settings", {"max_packets": 20, "max_detail_packets": 20})
        self.assertEqual(status, 200)
        status, result = self.api("/api/settings", {"max_packets": 10})
        self.assertEqual(status, 200)
        self.assertEqual(result["max_detail_packets"], 10)
        status, result = self.api("/api/settings", {"max_packets": 30})
        self.assertEqual(status, 200)
        self.assertEqual(result["max_detail_packets"], 30)
        self.assertIsNone(self.server.store.max_detail_count)

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
        for limit in [1, 5, 9, 99]:
            with self.subTest(limit=limit):
                status, result = self.api("/api/settings", {"capacity_retry_max_retries": limit})
                self.assertEqual(status, 200)
                self.assertEqual(result["capacity_retry_max_retries"], limit)
                saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
                self.assertEqual(saved["capacity_retry_max_retries"], limit)

    def test_native_retry_advice_uses_live_wait_setting_and_never_replays(self):
        self.api("/api/settings", {"capacity_retry_enabled": True, "capacity_retry_delay_seconds": 7})
        prefix = CREATED + frame("response.output_text.delta", delta="already delivered")
        for delay in (7, 2):
            self.api("/api/settings", {"capacity_retry_delay_seconds": delay})
            with patch.object(self.server.local_opener, "open", return_value=Response(prefix + FAILED)) as opened:
                status, data = self.relay(b"{}")
            self.assertEqual(status, 200)
            self.assertTrue(data.startswith(prefix))
            self.assertIn(f'"Retry-After":"{delay}"'.encode(), data[len(prefix):])
            self.assertEqual(opened.call_count, 1)
            meta, _ = self.latest()
            self.assertTrue(meta["native_retry_advised"])
            self.assertEqual(meta["capacity_retry_count"], 0)
            self.assertEqual(meta["capacity_retry_diagnostic"]["attempt_count"], 1)

    def test_invalid_wait_setting_does_not_partially_save(self):
        before = dict(self.server.settings)
        for delay in (0, -1, 100, True, 1.5, "3", None):
            status, _ = self.api("/api/settings", {"capacity_retry_enabled": True, "capacity_retry_delay_seconds": delay})
            self.assertEqual(status, 400)
            self.assertEqual(self.server.settings, before)

    def test_invalid_retry_limit_rejects_entire_settings_change(self):
        before = self.server.settings_path.read_bytes()
        for limit in [0, -1, 100, 101, True, False, "3", 2.5, None]:
            with self.subTest(limit=limit):
                status, _ = self.api("/api/settings", {"capacity_retry_enabled": True,
                                                      "capacity_retry_max_retries": limit})
                self.assertEqual(status, 400)
                self.assertFalse(self.server.settings["capacity_retry_enabled"])
                self.assertEqual(self.server.settings["capacity_retry_max_retries"], 3)
                self.assertEqual(self.server.settings_path.read_bytes(), before)

    def test_loading_previous_retry_limits_preserves_other_settings(self):
        for limit, expected in [(3, 3), (9, 9), (12, 12), (99, 99), (100, 99)]:
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
            status, result = self.api("/api/settings", {"start_on_login": True, "capacity_retry_enabled": True})
        self.assertEqual(status, 400, result)
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


    def test_disabled_retry_passes_failure_unchanged(self):
        with patch.object(self.server.local_opener, "open", return_value=Response(CREATED + FAILED)) as opened:
            status, data = self.relay(b"{}")
        self.assertEqual((status, data, opened.call_count), (200, CREATED + FAILED, 1))
        diagnostic = self.latest()[0]["capacity_retry_diagnostic"]
        self.assertEqual((diagnostic["outcome"], diagnostic["stop_reason"], diagnostic["retry_count"]), ("capacity_error", "disabled", 0))
        self.assertFalse(diagnostic["enabled"])



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
