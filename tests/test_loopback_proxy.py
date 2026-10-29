"""Proxy writes use fake Windows settings; the native smoke check is read-only."""
from copy import deepcopy
import ctypes
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector.loopback_proxy import (
    ENV_LOCAL, WINDOWS_LOCAL, ProxyRepairError, WindowsProxySettings, append_entries, repair_loopback_proxy)
from packet_inspector.server import InspectorServer


class FakeWindows:
    def __init__(self, **overrides):
        self.value = {"native": {"flags": 3, "proxy": "regional-proxy.example:8080", "bypass": "*.internal;<local>;127.0.0.1*", "pac": False},
                      "registry_bypass": ("*.internal;<local>;127.0.0.1*", 1),
                      "user_no_proxy": None, "machine_no_proxy": None, "process_no_proxy": None,
                      "environment_proxy": False, "registered_proxy": False, "process_proxy": False,
                      "machine_proxy_policy": False, **overrides}
        self.writes = []

    def read(self):
        return deepcopy(self.value)

    def write_bypass(self, before, value):
        self.writes.append(("ProxyOverride", value))
        self.value["native"]["bypass"] = value
        self.value["registry_bypass"] = (value, 1)

    def write_user_no_proxy(self, before, value):
        self.writes.append(("user_NO_PROXY", value))
        self.value["user_no_proxy"] = (value, (before["user_no_proxy"] or (None, 1))[1])

    def write_process_no_proxy(self, before, value):
        self.writes.append(("process_NO_PROXY", value))
        self.value["process_no_proxy"] = value

    def notify(self, proxy, environment):
        self.writes.append(("notify", proxy, environment))
        return {"proxy": proxy, "environment": environment}


class ProxyRepairTests(unittest.TestCase):
    def test_manual_proxy_preserves_existing_rules_and_is_idempotent(self):
        backend = FakeWindows()
        original = backend.read()
        report = repair_loopback_proxy(backend)
        self.assertEqual(report["state"], "repaired")
        expected = original["native"]["bypass"] + ";127.0.0.1;localhost;[::1]"
        self.assertEqual(backend.value["native"]["bypass"], expected)
        self.assertEqual(backend.value["native"]["proxy"], original["native"]["proxy"])
        self.assertEqual(backend.value["native"]["flags"], original["native"]["flags"])
        self.assertIsNone(backend.value["user_no_proxy"], "Do not introduce an environment override for a Windows-only proxy")
        writes = backend.writes.copy()
        self.assertEqual(repair_loopback_proxy(backend)["state"], "ok")
        self.assertEqual(backend.writes, writes)

    def test_environment_scopes_keep_their_own_rules(self):
        backend = FakeWindows(environment_proxy=True, registered_proxy=True, process_proxy=True,
            user_no_proxy=("user.internal", 2), machine_no_proxy=("*", 1), process_no_proxy="inherited.internal")
        repair_loopback_proxy(backend)
        self.assertEqual(backend.value["user_no_proxy"], ("user.internal," + ",".join(ENV_LOCAL), 2))
        self.assertEqual(backend.value["process_no_proxy"], "inherited.internal," + ",".join(ENV_LOCAL))
        self.assertEqual(backend.value["machine_no_proxy"], ("*", 1))
        self.assertNotIn("inherited.internal", backend.value["user_no_proxy"][0])
        self.assertNotIn("user.internal", backend.value["process_no_proxy"])

    def test_new_user_environment_retains_effective_machine_exclusions(self):
        backend = FakeWindows(environment_proxy=True, registered_proxy=True,
                              machine_no_proxy=("machine.internal", 1))
        repair_loopback_proxy(backend)
        self.assertEqual(backend.value["user_no_proxy"][0], "machine.internal," + ",".join(ENV_LOCAL))
        self.assertIsNone(backend.value["process_no_proxy"], "Do not alter a process that uses only Windows rules")

    def test_no_proxy_does_not_change_settings_or_notify(self):
        backend = FakeWindows(native={"flags": 1, "proxy": "", "bypass": "", "pac": False}, registry_bypass=None)
        self.assertEqual(repair_loopback_proxy(backend)["state"], "not_needed")
        self.assertFalse(backend.writes)

    def test_already_sufficient_machine_no_proxy_does_not_create_user_override(self):
        backend = FakeWindows(native={"flags": 1, "proxy": "", "bypass": "", "pac": False},
            registry_bypass=None, environment_proxy=True, registered_proxy=True, machine_no_proxy=("*", 1))
        self.assertEqual(repair_loopback_proxy(backend)["state"], "ok")
        self.assertFalse(backend.writes)
        self.assertIsNone(backend.value["user_no_proxy"])

    def test_machine_policy_blocks_only_a_required_write(self):
        backend = FakeWindows(machine_proxy_policy=True)
        with self.assertRaises(ProxyRepairError) as raised:
            repair_loopback_proxy(backend)
        self.assertEqual(raised.exception.report["state"], "failed")
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        self.assertFalse(backend.writes)
        value = ";".join(WINDOWS_LOCAL)
        backend.value["native"]["bypass"] = value
        backend.value["registry_bypass"] = (value, 1)
        self.assertEqual(repair_loopback_proxy(backend)["state"], "ok")

    def test_partial_failure_records_verified_changes(self):
        backend = FakeWindows(environment_proxy=True, registered_proxy=True)
        backend.write_user_no_proxy = Mock(side_effect=PermissionError("environment is read-only"))
        with self.assertRaises(ProxyRepairError) as raised:
            repair_loopback_proxy(backend)
        changes = raised.exception.report["changes"]
        self.assertTrue(changes[0]["applied"])
        self.assertFalse(changes[1]["applied"])
        self.assertEqual(raised.exception.report["state"], "failed")

    def test_missing_exact_rules_are_appended_but_universal_rule_is_preserved(self):
        self.assertEqual(append_entries("*", WINDOWS_LOCAL, ";"), "*")
        self.assertEqual(append_entries("LOCALHOST;", WINDOWS_LOCAL, ";"), "LOCALHOST;127.0.0.1;[::1]")

    def test_stale_native_cache_does_not_resurrect_removed_exclusions(self):
        backend = FakeWindows(registry_bypass=("current.internal", 1))
        repair_loopback_proxy(backend)
        self.assertEqual(backend.value["native"]["bypass"], "current.internal;" + ";".join(WINDOWS_LOCAL))

    def test_native_setter_only_changes_bypass_and_checks_concurrent_edits(self):
        backend = WindowsProxySettings.__new__(WindowsProxySettings)
        old, new = "*.internal", "*.internal;" + ";".join(WINDOWS_LOCAL)
        backend.registry = SimpleNamespace(HKEY_CURRENT_USER=1)
        backend._native = Mock(side_effect=[{"bypass": old}, {"bypass": new}])
        backend._registry_value = Mock(side_effect=[(old, 1), (new, 1)])
        observed = []
        def setter(handle, flag, pointer, size):
            value = pointer._obj
            observed.append((flag, value.count, value.options[0].option, ctypes.wstring_at(value.options[0].value.string)))
            return True
        backend.internet = SimpleNamespace(InternetSetOptionW=setter)
        before = {"native": {"bypass": old}, "registry_bypass": (old, 1), "machine_proxy_policy": False}
        backend.write_bypass(before, new)
        self.assertEqual(observed, [(75, 1, 3, new)])
        backend._native = Mock(return_value={"bypass": "external edit"})
        backend._registry_value = Mock(return_value=(old, 1))
        with self.assertRaisesRegex(ValueError, "changed during"):
            backend.write_bypass(before, new)
        self.assertEqual(len(observed), 1)

    @unittest.skipUnless(os.name == "nt", "Windows read-only API smoke check")
    def test_real_windows_read_has_valid_layout_without_modifying_settings(self):
        backend = WindowsProxySettings()
        with patch.object(backend, "write_bypass", side_effect=AssertionError("must not write")), \
             patch.object(backend, "write_user_no_proxy", side_effect=AssertionError("must not write")):
            snapshot = backend.read()
        self.assertIsInstance(snapshot["native"]["flags"], int)
        self.assertIsInstance(snapshot["native"]["bypass"], str)


class ConnectRepairIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config.toml"
        self.config.write_text('model="test"\n', encoding="utf8")
        repair = patch("packet_inspector.server.repair_loopback_proxy", return_value={"state": "ok", "changed": False})
        self.repair = repair.start()
        self.addCleanup(repair.stop)
        probe = patch("packet_inspector.network_diagnostics.NetworkDiagnostics._probe")
        probe.start()
        self.addCleanup(probe.stop)
        self.server = InspectorServer(("127.0.0.1", 0), root / "data", self.config)
        self.addCleanup(self.server.server_close)

    def test_only_explicit_connect_repairs_and_audits(self):
        self.repair.assert_not_called()
        self.server.auto_sync_providers()
        self.repair.assert_not_called()
        self.server.set_connection(True)
        self.repair.assert_called_once()
        self.server.set_connection(False)
        self.server.auto_sync_providers()
        self.assertEqual(self.repair.call_count, 1)
        records = self.server.network_diagnostics.export()["records"]
        self.assertEqual(records[0]["loopback_proxy"], {"state": "ok", "changed": False})

    def test_proxy_only_change_requires_restart_even_when_provider_matches(self):
        self.server.set_connection(True)
        self.repair.return_value = {"state": "repaired", "changed": True}
        result = self.server.set_connection(True)
        self.assertFalse(result["changed"])
        self.assertTrue(result["reload_required"])
        self.assertTrue(self.server.connection_reload_required)

    def test_repair_failure_leaves_codex_config_and_connection_untouched(self):
        original = self.config.read_bytes()
        self.repair.side_effect = ProxyRepairError("test repair failure", {"state": "failed", "changed": False})
        with self.assertRaises(ProxyRepairError):
            self.server.set_connection(True)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.server.connection_profile()["active"])
        self.assertFalse(self.server.settings["auto_detect_providers"])
        self.server.auto_sync_providers()
        self.assertEqual(self.config.read_bytes(), original, "Background sync must not bypass a failed precheck")
        records = self.server.network_diagnostics.export()["records"]
        self.assertEqual(records[0]["outcome"], "failed")
        self.assertEqual(records[0]["loopback_proxy"]["state"], "failed")

    def test_repair_audit_redacts_credentials_and_keeps_the_failure_cause(self):
        self.repair.return_value = {"state": "repaired", "changed": True,
            "changes": [{"setting": "user_NO_PROXY", "before": "https://user:secret@bad.example/path?token=private", "after": "localhost"}]}
        self.server.set_connection(True)
        value = str(self.server.network_diagnostics.export()["records"])
        self.assertNotIn("secret", value)
        self.assertNotIn("token=private", value)
        self.assertIn("localhost", value)


if __name__ == "__main__":
    unittest.main()
