"""Connection lifecycle tests use temporary configuration and no cloud requests."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.providers import ProviderManager
from packet_inspector.server import InspectorServer


class ConnectionPolicyTests(unittest.TestCase):
    def setUp(self):
        wsl = patch("packet_inspector.wsl_connection.LocalWsl.running", return_value=[])
        wsl.start()
        self.addCleanup(wsl.stop)
        repair = patch("packet_inspector.server.repair_loopback_proxy", return_value={"state": "ok", "changed": False})
        repair.start()
        self.addCleanup(repair.stop)
        probe = patch("packet_inspector.network_diagnostics.NetworkDiagnostics._probe")
        probe.start()
        self.addCleanup(probe.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n', encoding="utf8")
        self.link = ConfigLink(self.config, self.root / "connection.json")
        self.endpoint = "http://127.0.0.1:47844/relay/test"
        self.manager = ProviderManager(self.link, self.endpoint, "https://chatgpt.com/backend-api/codex")
        self.routes = {}

    def sync(self):
        return self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes))

    def data(self, path=None):
        return tomllib.loads((path or self.config).read_text(encoding="utf8"))

    def custom_config(self):
        self.config.write_text('model_provider="old"\nmodel="test"\n'
            '[model_providers.old]\nname="ChatGPT HTTP only"\n'
            'base_url="https://chatgpt.com/backend-api/codex"\n'
            'requires_openai_auth=true\nwire_api="responses"\n', encoding="utf8")

    def test_connect_status_disconnect_and_reconnect_with_optional_http_provider(self):
        self.config.write_text('model="test"\n[model_providers.local]\nbase_url="http://127.0.0.1:8787/v1"\n'
            '[model_providers.optional]\nbase_url="http://remote.invalid/v1"\nwire_api="responses"\n', encoding="utf8")
        with InspectorServer(("127.0.0.1", 0), self.root / "data", self.config, "https://chatgpt.com/backend-api/codex") as server:
            for _ in range(2):
                result = server.set_connection(True)
                status = server.status()
                self.assertTrue(result["active"])
                self.assertEqual(status["connection_state"], "connected")
                self.assertFalse(status["profile"].get("config_error"))
                self.assertFalse(status["provider_sync_error"])
                self.assertEqual(status["provider_warnings"][0]["provider"], "optional")
                self.assertEqual(self.data()["model_providers"]["optional"]["base_url"], "http://remote.invalid/v1")
                server.set_connection(False)
                self.assertEqual(server.status()["connection_state"], "disconnected")

    def test_first_connection_disconnect_and_reconnect_keep_one_stable_identity(self):
        history = self.root / "sessions" / "untouched.jsonl"
        history.parent.mkdir()
        history.write_bytes(b'{"history":"must not change"}\n')
        self.assertTrue(self.sync()["reload_required"])
        data = self.data()
        self.assertEqual(data["model_provider"], PROVIDER)
        canonical = data["model_providers"][PROVIDER]
        self.assertEqual(canonical["name"], "OpenAI")
        self.assertFalse(data["features"]["enable_request_compression"])
        relay_url = canonical["base_url"]
        route_count = len(self.routes)
        self.assertTrue(relay_url.startswith(self.endpoint + "/p/"))
        self.assertFalse(self.sync()["changed"])
        for _ in range(3):
            self.link.restore()
            direct = self.data()
            self.assertEqual(direct["model_provider"], PROVIDER)
            self.assertEqual(direct["model_providers"][PROVIDER]["base_url"], "https://chatgpt.com/backend-api/codex")
            self.assertTrue(direct["features"]["enable_request_compression"])
            self.assertFalse(self.link.profile()["active"])
            self.sync()
            self.assertEqual(self.data()["model_providers"][PROVIDER]["base_url"], relay_url)
            self.assertEqual(len(self.routes), route_count)
            self.assertFalse(self.data()["features"]["enable_request_compression"])
            self.assertTrue(all(not r["upstream"].startswith(self.endpoint) for r in self.routes.values()))
        self.assertEqual(history.read_bytes(), b'{"history":"must not change"}\n')

    def test_old_chat_alias_and_authentication_survive_both_states(self):
        self.custom_config()
        self.sync()
        self.assertEqual(self.data()["model_provider"], PROVIDER)
        self.assertEqual(self.data()["model_providers"]["old"]["name"], "OpenAI")
        self.link.restore()
        providers = self.data()["model_providers"]
        for provider_id in ("old", PROVIDER):
            self.assertEqual(providers[provider_id]["name"], "OpenAI")
            self.assertTrue(providers[provider_id]["requires_openai_auth"])
            self.assertEqual(providers[provider_id]["base_url"], "https://chatgpt.com/backend-api/codex")

    def test_independent_profiles_and_embedded_feature_overrides_follow_both_states(self):
        self.custom_config()
        self.config.write_text(self.config.read_text() + '\n[features]\nother_feature=true\n'
            'enable_request_compression=true\n[profiles.work.features]\nenable_request_compression=true\n')
        profile = self.root / "work.config.toml"
        profile.write_text('model_provider="old"\n[model_providers.old]\n'
            'base_url="https://profile.invalid/backend-api/codex"\n'
            '[profiles.local.features]\nenable_request_compression=false\n')
        self.sync()
        for path in (self.config, profile):
            data = self.data(path)
            self.assertFalse(data["features"]["enable_request_compression"])
            for value in data["profiles"].values():
                self.assertFalse(value["features"]["enable_request_compression"])
        self.link.restore()
        for path in (self.config, profile):
            data = self.data(path)
            self.assertTrue(data["features"]["enable_request_compression"])
            for value in data["profiles"].values():
                self.assertTrue(value["features"]["enable_request_compression"])
        self.assertTrue(self.data()["features"]["other_feature"])
        self.assertEqual(self.data(profile)["model_providers"][PROVIDER]["base_url"],
                         "https://profile.invalid/backend-api/codex")
        self.sync()
        self.assertFalse(self.sync()["changed"])

    def test_disconnect_enables_native_even_if_original_flag_was_disabled(self):
        self.config.write_text('[features]\nenable_request_compression=false\n')
        self.sync()
        self.link.restore()
        self.assertTrue(self.data()["features"]["enable_request_compression"])

    def test_profile_added_after_connection_keeps_its_own_upstream(self):
        self.sync()
        profile = self.root / "later.config.toml"
        profile.write_text('model_provider="later"\n[model_providers.later]\n'
            'name="Later Codex connection"\nrequires_openai_auth=true\n'
            'base_url="https://later.invalid/backend-api/codex"\n')
        self.sync()
        self.assertEqual(self.data(profile)["model_provider"], PROVIDER)
        self.assertEqual(self.data(profile)["model_providers"][PROVIDER]["name"], "OpenAI")
        self.link.restore()
        self.assertEqual(self.data(profile)["model_providers"][PROVIDER]["base_url"], "https://later.invalid/backend-api/codex")
        self.sync()
        self.assertFalse(self.sync()["changed"])

    def test_legacy_endpoint_still_resolves_after_entry_port_change(self):
        self.link.activate(self.endpoint, "https://original.invalid/backend-api/codex")
        self.manager.endpoint = self.endpoint.replace(":47844/", ":47849/")
        self.sync()
        self.assertEqual(self.link.profile()["upstream"], "https://original.invalid/backend-api/codex")
        self.assertTrue(all(not value["upstream"].startswith("http://127.0.0.1") for value in self.routes.values()))
        self.link.restore()
        self.assertEqual(self.data()["model_providers"][PROVIDER]["base_url"], "https://original.invalid/backend-api/codex")

    def test_api_key_provider_identity_and_credentials_are_preserved(self):
        self.config.write_text('model_provider="third_party"\n[model_providers.third_party]\n'
            'name="Third party"\nbase_url="https://example.invalid/v1"\nenv_key="TEST_KEY"\n')
        self.sync()
        self.assertEqual(self.data()["model_provider"], "third_party")
        self.assertNotIn(PROVIDER, self.data()["model_providers"])
        self.link.restore()
        provider = self.data()["model_providers"]["third_party"]
        self.assertEqual(provider["name"], "Third party")
        self.assertEqual(provider["env_key"], "TEST_KEY")
        self.assertEqual(provider["base_url"], "https://example.invalid/v1")

    def test_conflicting_canonical_provider_is_not_overwritten(self):
        self.custom_config()
        self.config.write_text(self.config.read_text() + f'\n[model_providers.{PROVIDER}]\n'
            'name="OpenAI"\nbase_url="https://another.invalid/backend-api/codex"\nrequires_openai_auth=true\n')
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "未覆盖已有会话"):
            self.sync()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.link.state_path.exists())

    def test_connection_write_failure_rolls_back_all_files_and_can_retry(self):
        self.custom_config()
        profile = self.root / "work.config.toml"
        profile.write_text('model="test-profile"\n')
        originals = {path: path.read_bytes() for path in (self.config, profile)}
        real_write = ConfigLink._write
        calls = []
        def fail_second(link, text):
            calls.append(link.config)
            if len(calls) == 2:
                raise OSError("simulated disk failure")
            return real_write(link, text)
        with patch.object(ConfigLink, "_write", fail_second), self.assertRaises(OSError):
            self.sync()
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.link._state())
        self.sync()
        self.assertTrue(self.link.profile()["active"])

    def test_disconnect_failure_preserves_active_routes_and_allows_retry(self):
        self.custom_config()
        profile = self.root / "work.config.toml"
        profile.write_text('model="profile"\n')
        self.sync()
        originals = {path: path.read_bytes() for path in (self.config, profile)}
        real_write = ConfigLink._write
        calls = []
        def fail_second(link, text):
            calls.append(link.config)
            if len(calls) == 2:
                raise OSError("simulated disk failure")
            return real_write(link, text)
        with patch.object(ConfigLink, "_write", fail_second), self.assertRaises(OSError):
            self.link.restore()
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.link.profile()["active"])
        self.link.restore()
        self.assertFalse(self.link.profile()["active"])

    def test_concurrent_edit_during_a_failed_batch_is_preserved_and_recoverable(self):
        self.custom_config()
        profile = self.root / "work.config.toml"
        profile.write_text('model="profile"\n')
        real_write = ConfigLink._write
        def interrupt_second(link, text):
            if link.config == profile:
                self.config.write_text(self.config.read_text() + '\n[notice]\nuser_edit=true\n')
                raise OSError("simulated interruption after concurrent edit")
            return real_write(link, text)
        with patch.object(ConfigLink, "_write", interrupt_second), self.assertRaises(OSError):
            self.sync()
        self.assertTrue(self.data()["notice"]["user_edit"])
        self.assertTrue(self.link._state())
        self.link.restore()
        self.assertTrue(self.data()["notice"]["user_edit"])
        self.assertFalse(self.link.profile()["active"])

    def test_failed_backend_disconnect_restores_connected_mode(self):
        self.custom_config()
        server = InspectorServer(("127.0.0.1", 0), self.root / "data", self.config)
        self.addCleanup(server.server_close)
        server.set_connection(True)
        before = self.config.read_bytes()
        with patch.object(ConfigLink, "_write", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                server.set_connection(False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(server.settings["auto_detect_providers"])
        self.assertEqual(server.status()["connection_state"], "connected")
        server.set_connection(False)
        self.assertFalse(server.settings["auto_detect_providers"])

    def test_backend_state_migration_and_repeated_connection_actions(self):
        self.custom_config()
        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "settings.json").write_text(json.dumps({"request_compression": "off", "zstd_level": 13}))
        server = InspectorServer(("127.0.0.1", 0), data_dir, self.config)
        self.addCleanup(server.server_close)
        self.assertEqual(server.status()["connection_state"], "disconnected")
        self.assertNotIn("request_compression", server.settings)
        server.set_connection(True)
        self.assertEqual(server.status()["connection_state"], "connected")

        self.assertFalse(server.set_connection(True)["reload_required"])
        server.set_connection(False)
        self.assertEqual(server.status()["connection_state"], "disconnected")
        self.assertFalse(server.set_connection(False)["reload_required"])
        self.assertEqual(server.settings["zstd_level"], 13)
        self.assertTrue(self.data()["features"]["enable_request_compression"])
        server.set_connection(True)
        self.assertEqual(server.status()["connection_state"], "connected")

    @unittest.skipUnless(shutil.which("node"), "Node.js required for frontend interaction checks")
    def test_two_state_frontend_interactions(self):
        result = subprocess.run([shutil.which("node"), "tests/connection_controls.cjs"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
