"""Controlled sharing interruption uses runtime APIs, never credential rewrites."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.sharing_upgrade import SharingUpgrade, sharing_state


class SharingUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.target = "1" * 32, "2" * 32
        self.shared = {"host": {"running": True, "address": "192.0.2.10", "port": 47846},
                       "client": {"active": True, "active_id": "borrowed-quota"},
                       "egress": {"host": {"active": True, "address": "192.0.2.10", "port": 47847},
                                  "selection": {"mode": "peer", "active_id": "borrowed-network"}}}
        # Exercise every independent role, including a lazily opened callback
        # exit listener alongside borrowed quota. Secrets are never required.
        self.states = {61001: deepcopy(self.shared), 61002: {}}
        self.health = {61001: {"worker_id": self.source, "pid": 123},
                       61002: {"worker_id": self.target, "pid": 456}}
        self.calls, self.events = [], []
        self.plan = SharingUpgrade(self.root, self.source, self.shared, lambda *args: self.events.append(args))
        self.workers = patch("packet_inspector.sharing_upgrade.worker_info", side_effect=lambda _root, identity:
                             {"port": 61001 if identity == self.source else 61002})
        self.workers.start()
        self.addCleanup(self.workers.stop)
        self.api_patch = patch("packet_inspector.sharing_upgrade.local_json", side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def api(self, port, path, data=None, **_kwargs):
        if path == "/health":
            return deepcopy(self.health[port])
        self.assertEqual(path, "/api/sharing")
        state = self.states[port]
        if data:
            self.calls.append((port, deepcopy(data)))
            action = data["action"]
            if action == "client_disconnect":
                state["client"] = {"active": False}
            elif action == "host_stop":
                state["host"] = {"running": False}
            elif action == "egress_select":
                state.setdefault("egress", {})["selection"] = {"mode": "local"}
            elif action == "egress_stop":
                state.setdefault("egress", {})["host"] = {"active": False}
            elif action == "host_resume":
                state["host"] = {"running": True, "address": data["address"], "port": data["port"]}
            elif action == "egress_resume":
                state.setdefault("egress", {})["host"] = {"active": True, "address": data["address"], "port": data["port"]}
            elif action == "credential_use":
                if data["kind"] == "quota":
                    state["client"] = {"active": True, "active_id": data["id"]}
                else:
                    state.setdefault("egress", {})["selection"] = {"mode": "peer", "active_id": data["id"]}
            else:
                self.fail("Unexpected sharing mutation: " + action)
        return deepcopy(state)

    def legacy_runtime(self, **values):
        # Captured 0.8.1 contract: /health has worker_id but no PID; the runtime
        # file is named for that worker and contains only PID, port and version.
        self.health[61001] = {"ok": True, "app": "codex-packet-inspector", "version": "0.8.1", "worker_id": self.source}
        path = self.root / "router-workers" / (self.source + ".runtime.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"pid": 123, "port": 61001, "version": "0.8.1", **values}), encoding="utf-8")
        return path

    def test_081_process_record_supports_pause_stop_and_restore(self):
        self.legacy_runtime()
        self.plan.pause(self.source, check=True)
        self.assertEqual(self.plan.source_pid, 123)
        with patch("packet_inspector.sharing_upgrade.process_alive", side_effect=[True, False]), \
             patch("packet_inspector.sharing_upgrade.control_worker", return_value={}) as control:
            self.plan.stop_source()
        control.assert_called_once_with(self.root, self.source, "stop")
        self.plan.restore(self.target)
        self.assertEqual(sharing_state(self.states[61002]), sharing_state(self.shared))

    def test_current_health_needs_no_runtime_file(self):
        self.plan.pause(self.source, check=True)
        self.assertEqual(self.plan.source_pid, 123)
        self.assertFalse((self.root / "router-workers").exists())

    def test_legacy_runtime_errors_do_not_pause_or_disclose_file_contents(self):
        for contents, reason in ((None, "runtime_unreadable"), ("private malformed content", "runtime_unreadable"),
                                 ("[]", "runtime_port_mismatch"),
                                 (json.dumps({"pid": 123, "port": 61002}), "runtime_port_mismatch"),
                                 (json.dumps({"pid": 123, "port": "61001"}), "runtime_port_mismatch"),
                                 (json.dumps({"pid": 0, "port": 61001}), "invalid_pid"),
                                 (json.dumps({"pid": True, "port": 61001}), "invalid_pid")):
            with self.subTest(contents=contents):
                path = self.legacy_runtime()
                if contents is None:
                    path.unlink()
                else:
                    path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.plan.pause(self.source, check=True)
                self.assertFalse(self.plan.paused)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.events[-1][1]["reason"], reason)
                self.assertNotIn("private malformed content", str(self.events))

    def test_invalid_health_pid_cannot_be_hidden_by_valid_runtime(self):
        self.legacy_runtime()
        for pid in (False, "123", 0, -1):
            with self.subTest(pid=pid):
                self.health[61001]["pid"] = pid
                with self.assertRaisesRegex(ValueError, "无法确认待停止"):
                    self.plan.pause(self.source, check=True)
                self.assertFalse(self.plan.paused)
                self.assertEqual(self.calls, [])

    def test_process_identity_changes_are_rejected_before_mutation(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.plan.source_pid = None
                if legacy:
                    self.legacy_runtime()
                self.plan.api(self.source)
                if legacy:
                    self.legacy_runtime(pid=789)
                else:
                    self.health[61001]["pid"] = 789
                with self.assertRaisesRegex(ValueError, "服务已变化"):
                    self.plan.api(self.source, {"action": "host_stop"})
                self.assertEqual(self.calls, [])
                self.assertEqual(self.events[-1][1]["reason"], "pid_changed")
                self.health[61001]["pid"] = 123

    def test_rollback_can_restore_a_restarted_legacy_worker(self):
        self.legacy_runtime()
        self.plan.pause(self.source, check=True)
        self.legacy_runtime(pid=789)
        self.plan.restore(self.source)
        self.assertEqual(self.plan.source_pid, 789)
        self.assertEqual(sharing_state(self.states[61001]), sharing_state(self.shared))

    def test_pause_and_restore_preserve_all_roles_addresses_and_credential_ids(self):
        vault = self.root / "credentials.db"
        vault.write_bytes(b"unchanged credentials and ledger")
        self.plan.pause(self.source, check=True)
        self.assertEqual(sharing_state(self.states[61001]), {})
        self.plan.restore(self.target)
        self.assertEqual(sharing_state(self.states[61002]), sharing_state(self.shared))
        self.assertEqual(vault.read_bytes(), b"unchanged credentials and ledger")
        self.assertEqual([data["action"] for _, data in self.calls],
                         ["client_disconnect", "host_stop", "egress_select", "egress_stop",
                          "egress_resume", "credential_use", "host_resume", "credential_use"])
        self.assertEqual(self.events[-1], ("restored",))

    def test_sharing_edits_after_confirmation_do_not_pause_services(self):
        self.states[61001]["client"]["active_id"] = "a-new-user-choice"
        with self.assertRaisesRegex(ValueError, "状态已变化"):
            self.plan.pause(self.source, check=True)
        self.assertFalse(self.plan.paused)
        self.assertEqual(self.calls, [])

    def test_partial_pause_and_lost_response_can_be_rolled_back(self):
        real_api = self.api
        def lost_response(port, path, data=None, **kwargs):
            result = real_api(port, path, data, **kwargs)
            if data and data["action"] == "host_stop":
                raise TimeoutError("response lost after stop")
            return result
        with patch("packet_inspector.sharing_upgrade.local_json", side_effect=lost_response):
            with self.assertRaises(TimeoutError):
                self.plan.pause(self.source, check=True)
        self.assertTrue(self.plan.paused)
        self.plan.restore(self.source)
        self.assertEqual(sharing_state(self.states[61001]), sharing_state(self.shared))

    def test_stop_waits_for_busy_requests_then_for_the_process_exit(self):
        self.plan.source_pid = 123
        with patch("packet_inspector.sharing_upgrade.process_alive", side_effect=[True, True, True, False]) as alive, \
             patch("packet_inspector.sharing_upgrade.control_worker", side_effect=[ValueError("control HTTP 409"), {}]) as control, \
             patch("packet_inspector.startup_diagnostics.time.sleep", return_value=None):
            self.plan.stop_source()
        self.assertEqual(control.call_count, 2)
        self.assertEqual(alive.call_count, 4)
        self.assertEqual(self.events[-1], ("source_stopped",))

    def test_already_retired_source_is_not_stopped_again(self):
        self.plan.source_pid = 123
        with patch("packet_inspector.sharing_upgrade.process_alive", return_value=False), \
             patch("packet_inspector.sharing_upgrade.control_worker") as control:
            self.plan.stop_source()
        control.assert_not_called()

    def test_wrong_worker_identity_is_never_mutated(self):
        with patch("packet_inspector.sharing_upgrade.local_json", return_value={"worker_id": self.target, "pid": 456}):
            with self.assertRaisesRegex(ValueError, "服务已变化"):
                self.plan.pause(self.source, check=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events[-1][1]["reason"], "worker_id_mismatch")

    def test_unidentified_borrowing_cannot_be_silently_recreated(self):
        with self.assertRaisesRegex(ValueError, "无法确认借入凭据"):
            sharing_state({"client": {"active": True}})

    def test_source_shutdown_errors_are_not_retried_as_busy_requests(self):
        self.plan.source_pid = 123
        with patch("packet_inspector.sharing_upgrade.process_alive", return_value=True), \
             patch("packet_inspector.sharing_upgrade.control_worker", side_effect=ValueError("control HTTP 403")) as control:
            with self.assertRaisesRegex(ValueError, "403"):
                self.plan.stop_source()
        self.assertEqual(control.call_count, 1)


if __name__ == "__main__":
    unittest.main()
