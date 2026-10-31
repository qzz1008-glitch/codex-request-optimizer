"""Automatic connection reconciliation preserves user intent and relay ownership."""
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.server import InspectorServer
from tests.test_wsl_connection import FakeWsl


class ConnectionResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\nmodel_auto_compact_token_limit=250000\n', encoding="utf8")
        self.backend = FakeWsl(self.root)
        self.server = self.make_server()
        self.resume = self.server.connection_resume
        self.resume.ready = True
        self.repair = patch("packet_inspector.server.repair_loopback_proxy")
        self.repair_mock = self.repair.start()
        self.addCleanup(self.repair.stop)
        self.cloud_probe = patch("packet_inspector.network_diagnostics.NetworkDiagnostics._probe")
        self.cloud_mock = self.cloud_probe.start()
        self.addCleanup(self.cloud_probe.stop)

    def make_server(self, **kwargs):
        server = InspectorServer(("127.0.0.1", 0), self.root / "data", self.config, **kwargs)
        server.analysis_stop.set()
        server.analysis_thread.join(timeout=3)
        self.assertFalse(server.analysis_thread.is_alive())
        server.analysis_stop.clear()
        server.wsl.backend = self.backend
        self.addCleanup(server.server_close)
        return server

    def connected(self):
        self.server.save_settings({**self.server.settings, "zstd_level": 16, "budget_bytes": 812000})
        self.server.sync_providers()
        self.server.connection_reload_required = False

    def test_connected_startup_synchronizes_wsl_without_disconnecting_or_external_probes(self):
        self.connected()
        before = self.config.read_bytes()
        self.resume.step()
        event = self.resume.status()
        self.assertEqual(event["state"], "complete")
        self.assertTrue(event["result"]["reload_required"])
        self.assertTrue(self.server.connection_profile()["active"])
        self.assertEqual(self.config.read_bytes(), before)
        data = tomllib.loads(self.backend.config_path.read_text(encoding="utf8"))
        self.assertTrue(data["openai_base_url"].startswith(self.server.endpoint))
        self.assertFalse(data["features"]["enable_request_compression"])
        self.assertEqual(self.server.settings["zstd_level"], 16)
        self.assertEqual(self.server.settings["budget_bytes"], 812000)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8"))["model_auto_compact_token_limit"], 250000)
        self.repair_mock.assert_not_called()
        self.cloud_mock.assert_not_called()
        report = json.loads((self.root / "data" / "connection-resume.json").read_text(encoding="utf8"))
        self.assertEqual(report["id"], event["id"])

    def test_disconnected_installation_stays_disconnected_and_unchanged(self):
        self.server.save_settings({**self.server.settings, "auto_detect_providers": False})
        before = self.config.read_bytes(), self.backend.config_path.read_bytes()
        with patch.object(self.server.wsl, "apply") as apply:
            self.resume.step()
        apply.assert_not_called()
        self.assertFalse(self.resume.status()["result"]["active"])
        self.assertFalse(self.server.settings["auto_detect_providers"])
        self.assertEqual(before, (self.config.read_bytes(), self.backend.config_path.read_bytes()))

    def test_repeated_activation_needs_no_restart_when_configuration_is_unchanged(self):
        self.connected()
        self.resume.step()
        self.server.connection_reload_required = False
        self.resume.schedule()
        before = self.config.read_bytes(), self.backend.config_path.read_bytes()
        self.resume.step()
        self.assertFalse(self.resume.status()["result"]["reload_required"])
        self.assertEqual(before, (self.config.read_bytes(), self.backend.config_path.read_bytes()))

    def test_probe_failure_is_reported_and_retry_recovers_without_toggling_connection(self):
        self.connected()
        self.backend.probe_error = "relay_unreachable"
        self.resume.step()
        failed = self.resume.status()
        self.assertEqual(failed["state"], "attention")
        self.assertEqual(failed["result"]["wsl"]["warnings"][0]["reason"], "relay_unreachable")
        self.assertTrue(self.server.connection_profile()["active"])
        self.backend.probe_error = None
        self.resume.retry()
        self.resume.step()
        self.assertEqual(self.resume.status()["state"], "complete")
        self.assertNotEqual(self.resume.status()["id"], failed["id"])

    def test_standby_worker_waits_for_router_commit_and_does_not_wait_for_draining(self):
        self.connected()
        second = self.make_server(worker_id="2" * 32, public_port=self.server.public_port)
        second.connection_resume.ready = True
        state = self.root / "data" / "router-state.json"
        state.write_text(json.dumps({"active": "1" * 32}), encoding="utf8")
        second.worker_control({"action": "activate", "maintenance": False})
        before = self.backend.config_path.read_bytes()
        second.connection_resume.step()
        self.assertEqual(before, self.backend.config_path.read_bytes())
        self.assertEqual(second.connection_resume.status()["state"], "pending")
        state.write_text(json.dumps({"active": second.worker_id}), encoding="utf8")
        second.connection_resume.step()
        self.assertFalse(second.maintenance_enabled)
        self.assertEqual(second.connection_resume.status()["state"], "complete")
        self.assertTrue(second.connection_resume.status()["result"]["wsl"]["targets"])

    def test_manual_disconnect_cancels_pending_automatic_work(self):
        self.connected()
        self.resume.schedule()
        self.server.set_connection(False)
        self.resume.step()
        self.assertFalse(self.resume.pending)
        self.assertFalse(self.server.connection_profile()["active"])
        self.assertFalse(self.server.settings["auto_detect_providers"])
        with self.assertRaisesRegex(ValueError, "当前未接入"):
            self.resume.retry()

    def test_previously_known_wsl_is_synchronized_when_it_starts_later(self):
        self.connected()
        self.resume.step()
        self.server.connection_reload_required = False
        self.backend.distributions = []
        self.resume.schedule()
        self.resume.step()
        deferred = self.resume.status()
        self.assertEqual(self.resume.deferred_wsl, {"Ubuntu-Test"})
        self.assertFalse(deferred["result"]["reload_required"])
        self.backend.distributions = ["Ubuntu-Test"]
        self.resume.next_wsl_scan = 0
        with patch.object(self.backend, "probe", wraps=self.backend.probe) as probe:
            self.resume.step()
        probe.assert_called_once()
        self.assertFalse(self.resume.deferred_wsl)
        self.assertNotEqual(self.resume.status()["id"], deferred["id"])

    def test_no_automatic_work_before_serving_http(self):
        self.connected()
        self.resume.ready = False
        before = self.backend.config_path.read_bytes()
        self.resume.step()
        self.assertTrue(self.resume.pending)
        self.assertEqual(before, self.backend.config_path.read_bytes())

    def test_replaced_worker_cannot_publish_routes_after_a_slow_probe(self):
        self.connected()
        before = self.backend.config_path.read_bytes()
        routes = dict(self.server.settings["provider_routes"])
        with patch.object(self.backend, "probe", side_effect=lambda *_: setattr(self.server, "worker_state", "draining")):
            self.resume.step()
        self.assertEqual(self.backend.config_path.read_bytes(), before)
        self.assertEqual(self.server.settings["provider_routes"], routes)


if __name__ == "__main__":
    unittest.main()
