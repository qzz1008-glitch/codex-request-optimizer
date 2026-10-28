import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.providers import ProviderManager
from packet_inspector.server import InspectorServer
from tests.relay_fixture import HTTP_TIMEOUT, wait_for_idle


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.link = ConfigLink(self.config, self.root / "state.json")
        self.manager = ProviderManager(self.link, "http://127.0.0.1:47844/relay/test", "https://default.invalid/v1")
        self.routes = {}

    def sync(self):
        return self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes))

    def test_native_retry_limits_update_idempotently_and_restore_user_values(self):
        self.config.write_text('model_provider="a"\n[model_providers.a]\nbase_url="https://a.invalid/v1"\n'
                               'stream_max_retries=7\nrequest_max_retries=2\n')
        def sync(limit):
            return self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes), retry_limit=limit)
        sync(3)
        provider = tomllib.loads(self.config.read_text())["model_providers"]["a"]
        self.assertEqual((provider["stream_max_retries"], provider["request_max_retries"]), (3, 0))
        self.assertFalse(sync(3)["changed"])
        sync(99)
        self.assertEqual(tomllib.loads(self.config.read_text())["model_providers"]["a"]["stream_max_retries"], 99)
        sync(None)
        provider = tomllib.loads(self.config.read_text())["model_providers"]["a"]
        self.assertEqual((provider["stream_max_retries"], provider["request_max_retries"]), (7, 2))

    def test_disconnecting_restores_missing_retry_fields_and_keeps_concurrent_user_edits(self):
        self.config.write_text('model_provider="a"\n[model_providers.a]\nbase_url="https://a.invalid/v1"\n')
        self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes), retry_limit=9)
        text = self.config.read_text().replace('stream_max_retries = 9', 'stream_max_retries = 11')
        self.config.write_text(text)
        self.link.restore()
        provider = tomllib.loads(self.config.read_text())["model_providers"]["a"]
        self.assertEqual(provider["stream_max_retries"], 11)
        self.assertNotIn("request_max_retries", provider)

    def test_preserves_provider_identity_credentials_comments_and_profile_overrides(self):
        original = ('# config comment\nmodel_provider="a"\nmodel="m"\n'
                    '[model_providers.a]\nbase_url="https://a.invalid/v1" # endpoint comment\n'
                    'env_key="KEY_A"\nhttp_headers={"X-Header"="unchanged"}\n'
                    'query_params={"api-version"="2026"}\nsupports_websockets=true\n'
                    '[model_providers."b.with.dot"]\nbase_url="https://b.invalid/api"\n')
        self.config.write_text(original)
        profile = self.root / "work.config.toml"
        profile.write_text('model_provider="a"\n[model_providers.a]\nbase_url="https://profile.invalid/api"\nenv_key="PROFILE_KEY"\n')
        result = self.sync()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_provider"], "a")
        self.assertEqual(data["model_providers"]["a"]["http_headers"], {"X-Header": "unchanged"})
        self.assertEqual(data["model_providers"]["a"]["query_params"], {"api-version": "2026"})
        self.assertIn("# endpoint comment", self.config.read_text())
        self.assertIn("# config comment", self.config.read_text())
        self.assertFalse(data["model_providers"]["a"]["supports_websockets"])
        self.assertIn("openai_base_url", data)
        self.assertNotIn("openai", data["model_providers"])
        self.assertEqual(len(self.routes), 4)
        self.assertTrue(result["changed"])
        profile_data = tomllib.loads(profile.read_text())
        self.assertEqual(profile_data["model_providers"]["a"]["env_key"], "PROFILE_KEY")
        self.assertNotEqual(data["model_providers"]["a"]["base_url"], profile_data["model_providers"]["a"]["base_url"])
        self.assertTrue(self.link.profile()["active"])
        self.assertEqual(self.link.profile()["upstream"], "https://a.invalid/v1")
        self.assertNotIn("KEY_A", json.dumps(self.routes))
        self.assertFalse(self.sync()["changed"])
        self.assertEqual(len(self.routes), 4)

    def test_hot_reload_new_provider_and_restore_preserve_later_changes(self):
        self.config.write_text('model="before"\n[model_providers.a]\nbase_url="https://a.invalid/v1"\n')
        self.sync()
        updated = self.config.read_text().replace('model="before"', 'model="later"')
        updated += '\n[model_providers.b]\nbase_url="https://b.invalid/v1"\n'
        self.config.write_text(updated)
        self.sync()
        self.assertEqual(len(self.routes), 4)
        self.link.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model"], "later")
        self.assertNotIn("openai_base_url", data)
        self.assertEqual(data["model_providers"]["a"], {"base_url": "https://a.invalid/v1"})
        self.assertEqual(data["model_providers"]["b"], {"base_url": "https://b.invalid/v1"})

    def test_user_changes_original_upstream_are_captured_and_restored(self):
        self.config.write_text('[model_providers.a]\nbase_url="https://a.invalid"\n')
        self.sync()
        old_url = tomllib.loads(self.config.read_text())["model_providers"]["a"]["base_url"]
        self.config.write_text(self.config.read_text().replace(old_url, "https://new.invalid"))
        self.sync()
        self.link.restore()
        self.assertEqual(tomllib.loads(self.config.read_text())["model_providers"]["a"]["base_url"], "https://new.invalid")

    def test_existing_single_provider_migrates_without_changing_default_or_history(self):
        self.config.write_text('model="m"\n')
        self.link.activate(self.manager.endpoint, "https://legacy.invalid/v1")
        self.sync()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_provider"], PROVIDER)
        self.assertTrue(self.link.profile()["active"])
        self.assertEqual(self.link.profile()["upstream"], "https://legacy.invalid/v1")
        self.assertIn("https://legacy.invalid/v1", {route["upstream"] for route in self.routes.values()})
        self.link.restore()
        restored = tomllib.loads(self.config.read_text())
        self.assertEqual(restored["model"], "m")
        self.assertEqual(restored["model_provider"], PROVIDER)
        self.assertEqual(restored["model_providers"][PROVIDER]["base_url"], "https://legacy.invalid/v1")
        self.assertTrue(restored["features"]["enable_request_compression"])

    def test_recovery_and_routes_exist_before_partial_config_failure(self):
        self.config.write_text('model="m"\n')
        def fail_write(link, text):
            self.assertTrue(self.routes)
            self.assertTrue(self.link.state_path.exists())
            raise OSError("disk failure")
        with patch.object(ConfigLink, "_write", fail_write), self.assertRaises(OSError):
            self.sync()
        self.assertEqual(tomllib.loads(self.config.read_text()), {"model": "m"})
        self.assertTrue(self.sync()["changed"])
        self.link.restore()
        restored = tomllib.loads(self.config.read_text())
        self.assertEqual(restored["model"], "m")
        self.assertEqual(restored["model_provider"], PROVIDER)
        self.assertTrue(restored["features"]["enable_request_compression"])

    def test_restore_retains_legacy_provider_referenced_by_saved_or_archived_sessions(self):
        for archived in [False, True]:
            with self.subTest(archived=archived):
                self.config.write_text('model="m"\n')
                self.link.activate(self.manager.endpoint, "https://legacy.invalid/v1")
                with closing(sqlite3.connect(self.root / "state_5.sqlite")) as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS threads (model_provider TEXT, archived INTEGER)")
                    connection.execute("DELETE FROM threads")
                    connection.execute("INSERT INTO threads VALUES (?, ?)", (PROVIDER, archived))
                    connection.commit()
                self.sync()
                self.link.restore()
                data = tomllib.loads(self.config.read_text())
                self.assertEqual(data["model_provider"], PROVIDER)
                self.assertEqual(data["model_providers"][PROVIDER]["base_url"], "https://legacy.invalid/v1")
                self.assertFalse(data["model_providers"][PROVIDER]["supports_websockets"])
                self.assertFalse(self.link.profile()["active"])
                self.sync()
                self.assertIn("/p/", tomllib.loads(self.config.read_text())["model_providers"][PROVIDER]["base_url"])
                self.link.restore()

    def test_restore_keeps_legacy_auth_when_the_session_index_is_unavailable(self):
        self.config.write_text('model_provider="custom"\n[model_providers.custom]\n'
                               'base_url="https://custom.invalid/v1"\nenv_key="PROVIDER_API_KEY"\n'
                               'http_headers={"X-Required"="fixture"}\n')
        self.link.activate(self.manager.endpoint, "https://custom.invalid/v1")
        (self.root / "state_5.sqlite").write_bytes(b"unavailable database")
        result = self.link.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertTrue(result["retained_provider"])
        retained = data["model_providers"][PROVIDER]
        self.assertEqual(retained["base_url"], "https://custom.invalid/v1")
        self.assertEqual(retained["env_key"], "PROVIDER_API_KEY")
        self.assertEqual(retained["http_headers"], {"X-Required": "fixture"})
        self.config.write_text(self.config.read_text().replace('model_provider = "custom"',
                                                             'model_provider = "' + PROVIDER + '"'))
        self.assertFalse(self.link.profile()["active"])

    def test_existing_routes_without_recovery_restore_real_upstreams(self):
        self.config.write_text('model_provider="a"\n[model_providers.a]\n'
                               'base_url="https://a.invalid/v1"\n')
        self.sync()
        self.link.state_path.unlink()
        self.sync()
        self.link.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_providers"]["a"]["base_url"], "https://a.invalid/v1")
        self.assertFalse(data["openai_base_url"].startswith(self.manager.endpoint))

    def test_restore_keeps_provider_referenced_by_a_profile_or_unindexed_history(self):
        for reference in ["named_profile", "profile_file", "unindexed_history"]:
            with self.subTest(reference=reference):
                self.config.write_text('model="m"\n')
                self.link.activate(self.manager.endpoint, "https://legacy.invalid/v1")
                if reference == "named_profile":
                    self.config.write_text(self.config.read_text() + '\n[profiles.saved]\nmodel_provider="' + PROVIDER + '"\n')
                elif reference == "profile_file":
                    (self.root / "saved.config.toml").write_text('model_provider="' + PROVIDER + '"\n')
                else:
                    (self.root / "sessions").mkdir()
                    (self.root / "sessions" / "fixture.jsonl").write_text('saved session must stay unchanged\n')
                result = self.link.restore()
                self.assertTrue(result["retained_provider"])
                retained = tomllib.loads(self.config.read_text())["model_providers"][PROVIDER]
                self.assertEqual(retained["base_url"], "https://legacy.invalid/v1")
                (self.root / "saved.config.toml").unlink(missing_ok=True)
        self.assertEqual((self.root / "sessions" / "fixture.jsonl").read_text(), 'saved session must stay unchanged\n')

    def test_restoring_migrated_legacy_preserves_later_default_provider(self):
        self.config.write_text('model="m"\n[model_providers.a]\nbase_url="https://a.invalid"\n')
        self.link.activate(self.manager.endpoint, "https://legacy.invalid")
        self.sync()
        self.config.write_text(self.config.read_text().replace('model_provider = "codex_packet_inspector"', 'model_provider = "a"'))
        self.link.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_provider"], "a")
        self.assertIn(PROVIDER, data["model_providers"])
        self.assertEqual(data["model_providers"]["a"]["base_url"], "https://a.invalid")

    def test_native_login_mode_change_keeps_explicit_override_but_updates_implicit_default(self):
        self.config.write_text('model="m"\n')
        self.sync()
        (self.root / "auth.json").write_text('{"auth_mode":"apikey"}')
        self.sync()
        root_url = tomllib.loads(self.config.read_text())["openai_base_url"]
        route_id = root_url.rsplit("/", 1)[1]
        self.assertEqual(self.routes[route_id]["upstream"], "https://api.openai.com/v1")
        self.link.restore()
        self.assertNotIn("openai_base_url", tomllib.loads(self.config.read_text()))

    def test_unknown_local_route_rejected_before_config_mutation(self):
        original = '[model_providers.a]\nbase_url="http://127.0.0.1:47844/relay/unknown"\n'
        self.config.write_text(original)
        with self.assertRaises(ValueError):
            self.sync()
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(self.routes)

    def test_profile_named_main_config_is_only_processed_once(self):
        self.config = self.root / "worker.config.toml"
        self.link.config = self.config
        self.config.write_text('model="m"\n')
        result = self.sync()
        self.assertEqual(result["config_files"], 1)
        self.assertFalse(self.sync()["changed"])

    def test_inherited_profile_websocket_override_restored(self):
        self.config.write_text('[model_providers.a]\nbase_url="https://a.invalid"\n')
        profile = self.root / "work.config.toml"
        original = '[model_providers.a]\nsupports_websockets=true\n'
        profile.write_text(original)
        self.sync()
        self.assertFalse(tomllib.loads(profile.read_text())["model_providers"]["a"]["supports_websockets"])
        self.link.restore()
        self.assertTrue(tomllib.loads(profile.read_text())["model_providers"]["a"]["supports_websockets"])
        self.assertTrue(tomllib.loads(profile.read_text())["features"]["enable_request_compression"])


class ProviderRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.observed = [[], []]
        self.upstreams = []
        for observations in self.observed:
            class Upstream(BaseHTTPRequestHandler):
                def do_POST(handler, observations=observations):
                    body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                    observations.append((handler.path, handler.headers.get("Authorization"), body))
                    result = b'{"ok":true}'
                    handler.send_response(200)
                    handler.send_header("Content-Length", str(len(result)))
                    handler.end_headers()
                    handler.wfile.write(result)
                def log_message(self, *_args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.upstreams.append(server)
        config = root / "config.toml"
        config.write_text('model_provider="a"\n' + ''.join(
            f'[model_providers.{name}]\nbase_url="http://127.0.0.1:{server.server_port}/{name}"\n'
            for name, server in zip("ab", self.upstreams)))
        self.server = InspectorServer(("127.0.0.1", 0), root / "data", config,
                                      f"http://127.0.0.1:{self.upstreams[0].server_port}/legacy")
        self.server.sync_providers()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.config = config
        self.opener = build_opener(ProxyHandler({}))
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for server in [self.server, *self.upstreams]:
            server.shutdown()
            try:
                if server is self.server:
                    wait_for_idle(server)
            finally:
                server.server_close()
                if server is self.server:
                    server.analysis_thread.join(timeout=HTTP_TIMEOUT)

    def post(self, url, auth="Bearer test"):
        request = Request(url, data=b'{"input":[]}', headers={"Authorization": auth, "Content-Type": "application/json"})
        with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
            response.read()
            status = response.status
        wait_for_idle(self.server)
        return status

    def test_routes_generation_compaction_query_and_legacy_to_correct_upstream(self):
        providers = tomllib.loads(self.config.read_text())["model_providers"]
        self.assertEqual(self.post(providers["a"]["base_url"] + "/responses?api-version=2026", "Bearer a"), 200)
        self.assertEqual(self.post(providers["b"]["base_url"] + "/responses/compact", "Bearer b"), 200)
        self.assertEqual(self.post(self.server.endpoint + "/responses", "Bearer legacy"), 200)
        self.assertEqual([(row[0], row[1]) for row in self.observed[0]],
                         [("/a/responses?api-version=2026", "Bearer a"), ("/legacy/responses", "Bearer legacy")])
        self.assertEqual([(row[0], row[1]) for row in self.observed[1]], [("/b/responses/compact", "Bearer b")])
        self.assertTrue(all(row[2] == b'{"input":[]}' for rows in self.observed for row in rows))

    def test_unknown_route_does_not_fall_through_to_default(self):
        with self.assertRaises(HTTPError) as failure:
            self.post(self.server.endpoint + "/p/missing/responses")
        self.assertEqual(failure.exception.code, 404)
        self.assertEqual(self.observed, [[], []])

    def test_live_sync_adds_provider_without_restarting_listener(self):
        original_port = self.server.server_port
        self.config.write_text(self.config.read_text() + f'\n[model_providers.c]\nbase_url="http://127.0.0.1:{self.upstreams[1].server_port}/new"\n')
        self.post(self.server.ui_url.rstrip("/") + "/api/providers/sync")
        endpoint = tomllib.loads(self.config.read_text())["model_providers"]["c"]["base_url"]
        self.assertEqual(self.post(endpoint + "/responses"), 200)
        self.assertEqual(self.server.server_port, original_port)
        self.assertEqual(self.observed[1][0][0], "/new/responses")

    def test_upgrade_stop_refuses_to_interrupt_active_requests(self):
        self.server.active_requests = 1
        with self.assertRaises(HTTPError) as failure:
            self.post(self.server.ui_url.rstrip("/") + "/api/upgrade/stop")
        self.assertEqual(failure.exception.code, 409)
        self.assertFalse(self.server.upgrade_draining)
        self.server.active_requests = 0

    def test_same_service_exposes_readonly_session_metadata(self):
        with patch.object(self.server.session_summaries, "read", return_value={"state": "available", "sessions": []}) as read:
            with self.opener.open(self.server.ui_url + "api/session-summaries", timeout=3) as response:
                self.assertEqual(json.load(response), {"state": "available", "sessions": []})
            read.assert_called_once_with(self.server.cache_dir)
        with patch.object(self.server.session_summaries, "read", side_effect=OSError("not available")):
            with self.opener.open(self.server.ui_url + "api/session-summaries", timeout=3) as response:
                self.assertEqual(json.load(response)["state"], "unavailable")

    def test_same_service_exposes_request_token_lookup(self):
        self.post(self.server.endpoint + "/responses")
        packet = self.server.store.list()[0]
        expected = {"state": "available", "input_tokens": 123, "output_tokens": 7}
        with patch.object(self.server.token_windows, "lookup_ready", return_value=expected) as lookup:
            with self.opener.open(self.server.ui_url + "api/packets/" + packet["id"] + "/token-window", timeout=3) as response:
                self.assertEqual(json.load(response), expected)
            self.assertEqual(lookup.call_args.args[0]["id"], packet["id"])


if __name__ == "__main__":
    unittest.main()
