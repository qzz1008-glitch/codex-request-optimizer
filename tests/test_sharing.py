"""Isolated LAN fixtures: dummy authorization, no real models or native config."""
import base64
import http.client
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import tomllib
import tomlkit
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from packet_inspector.config_link import ConfigLink
from packet_inspector.egress_transport import EGRESS_HEADER, parse_egress_link
from packet_inspector.providers import ProviderManager
from packet_inspector.server import InspectorServer
from packet_inspector.sharing import SharingError, automatic_address, parse_link, pinned_opener, forwarding_headers
from packet_inspector.sharing_auth import NativeAuthorization
from packet_inspector.sharing_config import SharedProvider, SHARED_PROVIDER
from packet_inspector.sharing_role import MachineSharingRole
from packet_inspector.sharing_credentials import CLIENT_HEADER
from tests.relay_fixture import HTTP_TIMEOUT, wait_for_idle


def token(expiry):
    claims = {"exp": expiry, "https://api.openai.com/auth": {"chatgpt_account_id": "dummy-account"}}
    return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + ".signature"


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\nmodel_provider="original"\n[model_providers.original]\nbase_url="https://example.invalid/v1"\n')
        self.link = ConfigLink(self.config, self.root / "link.json")
        self.shared = SharedProvider(self.link, self.root / "sharing-provider.json")

    def test_restore_preserves_other_edits_and_no_remote_secret_is_stored(self):
        self.shared.activate("http://127.0.0.1:47844/relay/local/sharing/client")
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_provider"], SHARED_PROVIDER)
        self.assertFalse(data["model_providers"][SHARED_PROVIDER]["requires_openai_auth"])
        self.config.write_text(self.config.read_text() + '\n[extra]\nkeep=true\n')
        self.shared.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["model_provider"], "original")
        self.assertNotIn(SHARED_PROVIDER, data["model_providers"])
        self.assertTrue(data["extra"]["keep"])
        self.assertFalse(self.shared.path.exists())

    def test_provider_scans_and_native_retry_settings_do_not_recapture_shared_route(self):
        endpoint = "http://127.0.0.1:47844/relay/local"
        self.shared.activate(endpoint + "/sharing/client")
        routes = {}
        ProviderManager(self.link, endpoint, "https://chatgpt.com/backend-api/codex").sync(
            routes, lambda value: routes.update(value), retry_limit=10)
        provider = tomllib.loads(self.config.read_text())["model_providers"][SHARED_PROVIDER]
        self.assertEqual(provider["base_url"], endpoint + "/sharing/client")
        self.assertNotIn("stream_max_retries", provider)
        self.shared.restore()
        self.assertNotIn(SHARED_PROVIDER, tomllib.loads(self.config.read_text())["model_providers"])

    def test_crash_recovery_and_concurrent_provider_selection(self):
        self.shared.activate("http://127.0.0.1:47844/relay/local/sharing/client")
        document = tomlkit.parse(self.config.read_text())
        document["model_provider"] = "another"
        self.config.write_text(tomlkit.dumps(document))
        SharedProvider(self.link, self.shared.path).restore()
        self.assertEqual(tomllib.loads(self.config.read_text())["model_provider"], "another")

    def test_active_profile_is_selected_and_restored_with_other_settings_preserved(self):
        self.config.write_text('profile="work"\n[profiles.work]\nmodel_provider="original"\nmodel="test"\n')
        self.shared.activate("http://127.0.0.1:47844/relay/local/sharing/client")
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["profiles"]["work"]["model_provider"], SHARED_PROVIDER)
        self.shared.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["profiles"]["work"], {"model_provider": "original", "model": "test"})
        self.assertNotIn("model_provider", data)

    def test_absent_original_selection_and_reserved_provider_are_safe(self):
        self.config.write_text('model="test"\n')
        self.shared.activate("http://127.0.0.1:47844/relay/local/sharing/client")
        self.shared.restore()
        self.assertNotIn("model_provider", tomllib.loads(self.config.read_text()))
        self.config.write_text(f'[model_providers.{SHARED_PROVIDER}]\nname="user-owned"\n')
        original = self.config.read_bytes()
        with self.assertRaises(SharingError):
            self.shared.activate("http://127.0.0.1:47844/relay/local/sharing/client")
        self.assertEqual(self.config.read_bytes(), original)


    def test_active_and_image_profiles_share_the_connection_and_restore_only_owned_fields(self):
        self.config.write_text('profile="work"\nmodel_provider="root"\n[profiles.work]\nmodel_provider="work-old"\n'
                               '[profiles.isolated-vision]\nmodel_provider="image-old"\n')
        layer = self.root / 'isolated-vision.config.toml'
        layer.write_text('model_provider="image-external"\nmodel="keep"\n'
                         '[profiles.isolated-vision]\nmodel_provider="image-inline"\n')
        active = self.root / 'work.config.toml'
        active.write_text('model_provider="external-work"\n')
        self.shared.activate('http://127.0.0.1:47844/relay/local/sharing/client')
        for path in (self.config, layer, active):
            data = tomllib.loads(path.read_text())
            self.assertEqual(data['model_provider'], SHARED_PROVIDER)
            for values in data.get('profiles', {}).values():
                self.assertEqual(values['model_provider'], SHARED_PROVIDER)
        layer.write_text(layer.read_text().replace('model="keep"', 'model="changed"'))
        active.write_text('model_provider="user-new-choice"\n')
        self.shared.restore()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data['model_provider'], 'root')
        self.assertEqual(data['profiles']['isolated-vision']['model_provider'], 'image-old')
        restored = tomllib.loads(layer.read_text())
        self.assertEqual(restored['model_provider'], 'image-external')
        self.assertEqual(restored['profiles']['isolated-vision']['model_provider'], 'image-inline')
        self.assertEqual(restored['model'], 'changed')
        self.assertEqual(tomllib.loads(active.read_text())['model_provider'], 'user-new-choice')

    def test_multifile_activation_failure_rolls_back_and_legacy_recovery_is_supported(self):
        layer = self.root / 'isolated-vision.config.toml'
        layer.write_text('model_provider="image"\n')
        originals = {path: path.read_bytes() for path in (self.config, layer)}
        write = ConfigLink._write
        def fail_layer(link, text):
            if link.config == layer:
                raise OSError('dummy write failure')
            return write(link, text)
        with patch.object(ConfigLink, '_write', fail_layer), self.assertRaises(OSError):
            self.shared.activate('http://127.0.0.1:47844/relay/local/sharing/client')
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        layer.unlink()
        self.shared.activate('http://127.0.0.1:47844/relay/local/sharing/client')
        state = json.loads(self.shared.path.read_text())
        state.pop('layers')
        state.pop('profiles')
        self.shared.path.write_text(json.dumps(state))
        self.shared.restore()
        self.assertEqual(tomllib.loads(self.config.read_text())['model_provider'], 'original')


class AuthorizationTests(unittest.TestCase):
    def test_native_subprocess_protocol_uses_only_native_managed_authorization(self):
        dummy = token(time.time() + 3600)
        program = '''import sys,json,os
if os.environ.get("CODEX_HOME") != sys.argv[2]: sys.exit(7)
for line in sys.stdin:
 message=json.loads(line)
 if "id" not in message: continue
 result={} if message["method"]=="initialize" else {"authMethod":"chatgpt","authToken":sys.argv[1]}
 print(json.dumps({"id":message["id"],"result":result}),flush=True)
'''
        home = Path.cwd().resolve()
        broker = NativeAuthorization([sys.executable, "-u", "-c", program, dummy, str(home)], timeout=3, native_home=home)
        try:
            credentials = broker.credentials()
            self.assertEqual(credentials["headers"]["Authorization"], "Bearer " + dummy)
            self.assertEqual(credentials["headers"]["ChatGPT-Account-Id"], "dummy-account")
            process = broker.process
        finally:
            broker.close()
        self.assertIsNotNone(process.poll())

    def test_refresh_is_requested_from_native_interface_and_not_implemented_locally(self):
        broker = NativeAuthorization()
        with patch.object(broker, "_start"), patch.object(broker, "_call", side_effect=[
            {"authMethod": "chatgpt", "authToken": token(time.time() - 1)},
            {"authMethod": "chatgpt", "authToken": token(time.time() + 3600)}]) as call:
            broker.credentials()
        self.assertEqual(call.call_args_list[0].args, ("getAuthStatus", {"includeToken": True, "refreshToken": False}))
        self.assertEqual(call.call_args_list[1].args, ("getAuthStatus", {"includeToken": True, "refreshToken": True}))

    def test_unsupported_or_malformed_authorization_fails_closed(self):
        for result in [None, {}, {"authMethod": "unknown", "authToken": "dummy"},
                       {"authMethod": "chatgpt", "authToken": "bad"}, {"authMethod": "apikey", "authToken": "x\ny"}]:
            with self.subTest(result=result), self.assertRaises(SharingError):
                NativeAuthorization._credentials(result)


class DummyAuthorization:
    def __init__(self):
        self.identity = "dummy-owner"
        self.closed = False
        self.calls = 0

    def credentials(self):
        self.calls += 1
        return {"mode": "chatgpt", "identity": self.identity, "expiry": None,
                "headers": {"Authorization": "Bearer dummy-owner-secret", "ChatGPT-Account-Id": "dummy-owner-id"}}

    def close(self):
        self.closed = True


class LANSharingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.received = []
        self.release_stream = threading.Event()
        self.stream_started = threading.Event()
        self.gate_stream = False
        self.direct_response = None
        self.response_status = 200
        self.extra_response_headers = {}
        self.extra_relays = []
        fixture = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fixture.received.append((self.path, dict(self.headers), body))
                self.send_response(fixture.response_status)
                self.send_header("Content-Type", "application/json" if fixture.direct_response is not None else "text/event-stream")
                self.send_header("Set-Cookie", "owner-cookie=never-forward")
                for key, value in fixture.extra_response_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if fixture.direct_response is not None:
                    self.wfile.write(fixture.direct_response)
                    self.wfile.flush()
                    return
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"first"}\n\n')
                self.wfile.flush()
                fixture.stream_started.set()
                if fixture.gate_stream:
                    fixture.release_stream.wait(HTTP_TIMEOUT)
                self.wfile.write(fixture.completion)
                self.wfile.flush()

            def do_GET(self):
                body = b'{"models":[{"slug":"dummy-test-model"}]}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

        self.completion = b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":37,"output_tokens":2,"total_tokens":39}}}\n\n'
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.target = f"http://127.0.0.1:{self.upstream.server_port}"
        self.owner = self.make_relay("owner")
        self.guest = self.make_relay("guest")
        self.authorization = DummyAuthorization()
        self.owner.sharing.authorization_factory = lambda: self.authorization
        for server in (self.upstream, self.owner, self.guest):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()

    def make_relay(self, name):
        root = self.root / name
        root.mkdir()
        config = root / "config.toml"
        config.write_text('model="test"\n', encoding="utf8")
        server = InspectorServer(("127.0.0.1", 0), root / "data", config, self.target)
        server.settings["egress_port"] = free_port()
        # Owner and guest fixtures represent separate machines, even on one CI host.
        server.sharing.role = MachineSharingRole('CodexSharingTest-' + secrets.token_hex(16))
        return server

    def tearDown(self):
        self.release_stream.set()
        for server in (*reversed(self.extra_relays), self.guest, self.owner):
            wait_for_idle(server)
            server.shutdown()
            server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temporary.cleanup()

    def start(self, **options):
        with patch.object(self.owner.sharing, "_host_upstream", return_value=self.target):
            return self.owner.sharing.start_host("127.0.0.1", free_port(), **options)["link"]

    def remote_request(self, link, path="/v1/status", body=None, grant=True):
        host, port, token, fingerprint = parse_link(link)
        opener = pinned_opener(host, port, fingerprint)
        headers = {"Authorization": "Bearer " + token, CLIENT_HEADER: self.guest.sharing.vault.client_id} if grant else {}
        try:
            response = opener.open(Request(f"https://{host}:{port}" + path, data=body, headers=headers), timeout=HTTP_TIMEOUT)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def request_guest(self, path="/responses", headers=None, content="dummy prompt", guest=None):
        guest = guest or self.guest
        client = http.client.HTTPConnection("127.0.0.1", guest.port, timeout=HTTP_TIMEOUT)
        target = urlsplit(guest.sharing.client_endpoint).path
        body = json.dumps({"model": "test", "input": [{"role": "user", "content": content}]}).encode()
        client.request("POST", target + path, body, {"Content-Type": "application/json", **(headers or {})})
        return client, client.getresponse(), body

    def extra_relay(self, name):
        relay = self.make_relay(name)
        threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.extra_relays.append(relay)
        return relay

    def consume(self, **kwargs):
        client, response, _ = self.request_guest(**kwargs)
        try:
            result = response.read()
            self.assertEqual(response.status, 200, result)
        finally:
            client.close()

    def local_api(self, path, data):
        client = http.client.HTTPConnection("127.0.0.1", self.owner.port, timeout=HTTP_TIMEOUT)
        try:
            client.request("POST", path, json.dumps(data).encode(), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_automatic_binding_uses_saved_ports_without_interrupting_active_sharing(self):
        quota, exit_port = free_port(), free_port()
        while exit_port == quota:
            exit_port = free_port()
        status, result = self.local_api('/api/settings', {'sharing_port': quota, 'egress_port': exit_port})
        self.assertEqual(status, 200)
        self.assertTrue(result['sharing_settings_supported'])
        with patch('packet_inspector.sharing.automatic_address', return_value='127.0.0.1'), \
             patch('packet_inspector.shared_egress.automatic_address', return_value='127.0.0.1'), \
             patch.object(self.owner.sharing, '_host_upstream', return_value=self.target):
            status, host = self.local_api('/api/sharing', {'action':'host_start', 'request_limit':1})
            self.assertEqual(status, 200)
            self.assertEqual(host['host']['port'], quota)
            self.assertNotIn('request_limit', host['host'])
            status, shared_exit = self.local_api('/api/sharing', {'action':'egress_start'})
            self.assertEqual(status, 200)
            self.assertEqual(shared_exit['egress']['host']['port'], exit_port)
            for _ in range(2):
                self.assertEqual(self.remote_request(host['link'], '/v1/responses', b'{}')[0], 200)
            new_quota, new_exit = free_port(), free_port()
            while new_exit == new_quota:
                new_exit = free_port()
            status, _ = self.local_api('/api/settings', {'sharing_port': new_quota, 'egress_port': new_exit})
            self.assertEqual(status, 200)
            self.assertEqual(self.owner.sharing.listener.server_port, quota)
            self.assertEqual(self.owner.egress.listener.server_port, exit_port)
            self.assertEqual(self.remote_request(host['link'])[0], 200)
            saved = json.loads(self.owner.settings_path.read_text())
            self.assertEqual((saved['sharing_port'], saved['egress_port']), (new_quota, new_exit))
            self.owner.sharing.stop_host()
            self.owner.egress.stop()
            self.authorization = DummyAuthorization()
            self.assertEqual(self.local_api('/api/sharing', {'action':'host_start'})[1]['host']['port'], new_quota)
            self.assertEqual(self.local_api('/api/sharing', {'action':'egress_start'})[1]['egress']['host']['port'], new_exit)

    def test_invalid_sharing_ports_reject_the_whole_settings_update(self):
        previous = dict(self.owner.settings)
        invalid = [{'sharing_port': value} for value in (True, 0, 65536, '47846', 47846.0)]
        invalid += [{'egress_port': value} for value in (True, -1, 65536)]
        invalid += [{'sharing_port': 50001, 'egress_port': 50001}]
        for fields in invalid:
            with self.subTest(fields=fields):
                status, _ = self.local_api('/api/settings', {'capture_enabled': False, **fields})
                self.assertEqual(status, 400)
                self.assertEqual(self.owner.settings, previous)

    def test_one_quota_source_is_replaced_atomically_without_rewriting_provider(self):
        original = self.guest.link.config.read_bytes()
        first = self.start()
        self.guest.sharing.connect(first)
        connected = self.guest.link.config.read_bytes()
        other = self.extra_relay("another-owner")
        other.sharing.authorization_factory = DummyAuthorization
        with patch.object(other.sharing, "_host_upstream", return_value=self.target):
            second = other.sharing.start_host("127.0.0.1", free_port())["link"]
        with self.assertRaises(SharingError):
            self.guest.sharing.connect(second.rsplit(".", 1)[0] + "." + "0" * 64)
        self.assertEqual(self.guest.sharing.remote["port"], parse_link(first)[1])
        self.consume()
        with patch.object(self.guest.sharing.provider, "activate", side_effect=AssertionError("Replacement rewrote native config")):
            result = self.guest.sharing.connect(second)
        self.assertFalse(result["reload_required"])
        self.assertEqual(self.guest.link.config.read_bytes(), connected)
        self.consume()
        wait_for_idle(self.owner, self.guest, other)
        self.assertEqual(self.owner.sharing.status()["host"]["requests"], 1)
        self.assertEqual(other.sharing.status()["host"]["requests"], 1)
        with self.assertRaises(SharingError):
            self.guest.sharing.start_host("127.0.0.1", free_port())
        self.guest.sharing.disconnect()
        self.assertEqual(tomllib.loads(self.guest.link.config.read_text()), tomllib.loads(original.decode()))

    def test_multiple_borrowers_share_one_owner_and_only_aggregate_usage_is_kept(self):
        link = self.start()
        second = self.extra_relay("another-borrower")
        self.guest.sharing.connect(link)
        second.sharing.connect(link)
        self.consume(content="private-first-borrower")
        self.consume(guest=second, content="private-second-borrower")
        wait_for_idle(self.owner, self.guest, second)
        host = self.owner.sharing.status()["host"]
        self.assertEqual(host["requests"], 2)
        self.assertEqual(host["usage"]["total_tokens"], 78)
        outgoing = [row for row in self.owner.sharing.status()["credentials"] if row["direction"] == "out"]
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]["totals"]["total_tokens"], 78)
        self.assertEqual(self.owner.store.list(), [])
        self.assertTrue(self.guest.sharing.status()["client"]["active"])
        self.assertTrue(second.sharing.status()["client"]["active"])

    def test_credential_api_live_edits_history_selection_and_shared_machine_usage(self):
        self.owner.settings["sharing_port"] = free_port()
        with patch("packet_inspector.sharing.automatic_address", return_value="127.0.0.1"), patch.object(
                self.owner.sharing, "_host_upstream", return_value=self.target):
            status, issued = self.local_api("/api/sharing", {"action": "credential_issue", "kind": "quota",
                "label": "first colleague", "budget": "10", "weekly_reset": True, "expires_at": None})
        self.assertEqual(status, 200)
        identifier, link = issued["issued_id"], issued["link"]
        self.guest.sharing.connect(link, label="provider history")
        second = self.extra_relay("second-colleague")
        second.sharing.connect(link)
        self.consume()
        self.consume(guest=second)
        wait_for_idle(self.owner, self.guest, second)
        status, edited = self.local_api("/api/sharing", {"action": "credential_edit", "id": identifier,
            "budget": "25", "weekly_reset": False, "expires_at": time.time() + 7 * 86400})
        self.assertEqual(status, 200)
        row = next(row for row in edited["credentials"] if row["id"] == identifier)
        self.assertEqual(row["budget_nanos"], 25_000_000_000)
        self.assertFalse(row["weekly_reset"])
        self.assertEqual(row["totals"]["unpriced_responses"], 2)  # Dummy model has no published rate.
        self.assertEqual(row["totals"]["requests"], 2)
        self.guest.sharing.disconnect()
        saved = next(row for row in self.guest.sharing.status()["credentials"] if row["direction"] == "in")
        self.assertEqual(saved["label"], "provider history")
        self.guest.sharing.connect(identifier=saved["id"])
        self.assertEqual(self.guest.sharing.status()["client"]["active_id"], saved["id"])
        _, disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": identifier, "enabled": False})
        self.assertFalse(disabled["host"]["running"])
        self.assertNotIn(link, json.dumps(self.owner.sharing.status()))
        self.assertEqual(self.owner.store.list(), [])

    def test_credential_rows_control_lending_and_release_the_role_after_the_last_is_disabled(self):
        self.owner.settings["sharing_port"] = free_port()
        with patch("packet_inspector.sharing.automatic_address", return_value="127.0.0.1"), patch.object(
                self.owner.sharing, "_host_upstream", return_value=self.target):
            _, first = self.local_api("/api/sharing", {"action": "credential_issue", "kind": "quota", "label": "first"})
            _, second = self.local_api("/api/sharing", {"action": "credential_issue", "kind": "quota", "label": "second"})
            first_id, second_id = first["issued_id"], second["issued_id"]
            status, one_disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": False})
            self.assertEqual(status, 200)
            self.assertTrue(one_disabled["host"]["running"])
            self.assertEqual(self.remote_request(first["link"])[0], 403)
            self.assertEqual(self.remote_request(second["link"])[0], 200)
            _, all_disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": second_id, "enabled": False})
            self.assertFalse(all_disabled["host"]["running"])
            with patch.object(self.owner.sharing, "ensure_service", side_effect=AssertionError("Invalid edit started lending")):
                status, _ = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id,
                    "enabled": True, "expires_at": -1})
                self.assertEqual(status, 400)
            status, resumed = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": True})
            self.assertEqual(status, 200)
            self.assertTrue(resumed["host"]["running"])
            self.assertEqual(self.remote_request(first["link"])[0], 200)
            self.assertEqual(self.remote_request(second["link"])[0], 403)
            self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": False})
        other = self.extra_relay("another-quota-owner")
        other.sharing.authorization_factory = DummyAuthorization
        with patch.object(other.sharing, "_host_upstream", return_value=self.target):
            link = other.sharing.start_host("127.0.0.1", free_port())["link"]
        self.owner.sharing.connect(link)
        self.assertTrue(self.owner.sharing.status()["client"]["active"])
        status, _ = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": True})
        self.assertEqual(status, 400)
        self.assertFalse(next(row for row in self.owner.sharing.vault.rows() if row["id"] == first_id)["enabled"])

    def test_exit_row_controls_preserve_other_grants_and_internal_callback(self):
        with patch("packet_inspector.shared_egress.automatic_address", return_value="127.0.0.1"):
            _, first = self.local_api("/api/sharing", {"action": "credential_issue", "kind": "exit", "label": "first"})
            _, second = self.local_api("/api/sharing", {"action": "credential_issue", "kind": "exit", "label": "second"})
            first_id, second_id = first["issued_id"], second["issued_id"]
            _, one_disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": False})
            self.assertTrue(one_disabled["egress"]["host"]["active"])
            _, all_disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": second_id, "enabled": False})
            self.assertFalse(all_disabled["egress"]["host"]["active"])
            status, resumed = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": True})
            self.assertEqual(status, 200)
            self.assertTrue(resumed["egress"]["host"]["active"])
            self.owner.egress.issue(internal=True)
            _, disabled = self.local_api("/api/sharing", {"action": "credential_edit", "id": first_id, "enabled": False})
            self.assertTrue(disabled["egress"]["host"]["active"], "The internal callback still needs its listener")

    def test_saved_quota_links_survive_listener_restart_without_rebinding(self):
        link = self.start()
        self.guest.sharing.connect(link)
        host, port, _token, fingerprint = parse_link(link)
        self.owner.sharing.stop_host()
        self.authorization = DummyAuthorization()
        with patch.object(self.owner.sharing, "_host_upstream", return_value=self.target):
            self.owner.sharing.ensure_service(host, port)
        self.assertEqual(self.owner.sharing.host["fingerprint"], fingerprint)
        self.consume()

    def test_credential_storage_failure_does_not_change_native_provider(self):
        link = self.start()
        before = self.guest.link.config.read_bytes()
        with patch.object(self.guest.sharing.vault, "import_link", side_effect=OSError("storage unavailable")):
            with self.assertRaises(SharingError):
                self.guest.sharing.connect(link)
        self.assertEqual(self.guest.link.config.read_bytes(), before)
        self.assertIsNone(self.guest.sharing.remote)
        self.guest.sharing.connect(link)
        self.consume()

    def test_quota_provider_uses_borrower_exit_selection_instead_of_its_own_peer(self):
        self.guest.sharing.connect(self.start())
        foreign = "codex-exit://127.0.0.1:47847/#" + secrets.token_urlsafe(32) + "." + "a" * 64
        owner_routes = []
        physical = self.owner.opener
        def delegated(link, **_options):
            self.assertEqual(_options["client_id"], self.guest.sharing.vault.client_id)
            owner_routes.append(link)
            return physical
        self.owner.egress.opener_factory = delegated
        # An owner can use another exit for its own traffic. Borrowers choosing
        # the owner's exit still receive the owner's physical network.
        self.owner.egress.mode = "peer"
        self.owner.egress.peer_opener = None
        self.consume()
        self.assertEqual(owner_routes, [])
        self.guest.egress.select("local")
        self.consume()
        callback = owner_routes[-1]
        self.assertEqual(parse_egress_link(callback)[1], self.guest.egress.listener.server_port)
        # A local physical exit can be shared while this machine borrows quota.
        self.assertTrue(self.guest.egress.status()["host"]["active"])
        class Selected:
            def close(self):
                pass
        self.guest.egress.opener_factory = lambda *_args, **_kwargs: Selected()
        self.guest.egress.select("peer", foreign)
        self.consume()
        self.assertEqual(owner_routes, [callback, foreign])
        with patch.object(self.guest.egress, "opener_factory", side_effect=SharingError("unreachable")):
            with self.assertRaises(SharingError):
                self.guest.egress.select("peer", foreign.replace("47847", "47848"))
        self.assertEqual(self.guest.egress.peer_link, foreign)
        self.consume()
        self.assertEqual(owner_routes[-1], foreign)
        for _path, headers, _body in self.received:
            self.assertFalse(any(key.lower() == EGRESS_HEADER.lower() for key in headers))
        for path in self.guest.data_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(foreign.encode(), path.read_bytes())

    def test_own_quota_can_choose_one_shared_exit_without_borrowing_quota(self):
        logical = "https://api.openai.com/v1"
        self.guest.settings.update(upstream=logical, auto_detect_providers=False)
        self.guest.sharing.authorization_factory = DummyAuthorization
        with patch.object(self.guest.sharing, "_host_upstream", return_value=logical):
            self.guest.sharing.start_host("127.0.0.1", free_port())
        calls = []
        physical = self.guest.local_opener
        target = self.target
        class MappedExit:
            def open(self, request, **kwargs):
                calls.append(request.full_url)
                mapped = Request(target + urlsplit(request.full_url).path, data=request.data,
                                 headers=dict(request.header_items()), method=request.get_method())
                return physical.open(mapped, **kwargs)
            def close(self):
                pass
        self.guest.egress.opener_factory = lambda *_args, **_kwargs: MappedExit()
        foreign = "codex-exit://127.0.0.1:47847/#" + secrets.token_urlsafe(32) + "." + "a" * 64
        self.guest.egress.select("peer", foreign)
        self.guest.egress.start("127.0.0.1", 0)
        with patch.object(self.guest.opener, "open", side_effect=AssertionError("Ignored selected exit")):
            client = http.client.HTTPConnection("127.0.0.1", self.guest.port, timeout=HTTP_TIMEOUT)
            try:
                client.request("POST", "/relay/" + self.guest.settings["relay_token"] + "/responses",
                               b'{"model":"test","input":[]}',
                               {"Content-Type": "application/json", "Authorization": "Bearer dummy-own-account"})
                response = client.getresponse()
                self.assertEqual(response.status, 200, response.read())
            finally:
                client.close()
        self.assertEqual(calls, [logical + "/responses"])
        self.assertIsNone(self.guest.sharing.remote)
        self.assertIsNotNone(self.guest.sharing.listener)
        self.assertTrue(self.guest.egress.status()["host"]["active"])
        self.assertEqual(self.received[-1][1]["Authorization"], "Bearer dummy-own-account")

    def test_streams_immediately_and_substitutes_only_owner_authorization(self):
        link = self.start()
        self.guest.sharing.connect(link)
        self.owner.settings["image_isolation_enabled"] = True  # Host must not interpret guest image paths.
        self.gate_stream = True
        client, response, expected = self.request_guest(headers={"Authorization": "Bearer dummy-guest-secret",
            "ChatGPT-Account-Id": "dummy-guest-id", "OpenAI-Organization": "guest-org", "Cookie": "guest-cookie"})
        try:
            self.assertEqual(response.status, 200)
            chunk = response.read1(65536)
            self.assertIn(b'"delta":"first"', chunk)
            self.assertFalse(self.release_stream.is_set())
            self.release_stream.set()
            result = chunk + response.read()
            self.assertTrue(result.endswith(self.completion))
            self.assertIsNone(response.getheader("Set-Cookie"))
        finally:
            self.release_stream.set()
            client.close()
        wait_for_idle(self.owner, self.guest)
        self.assertEqual(len(self.received), 1)
        _, headers, body = self.received[0]
        self.assertEqual(headers["Authorization"], "Bearer dummy-owner-secret")
        self.assertEqual(headers["Chatgpt-Account-Id"], "dummy-owner-id")
        self.assertNotIn("Openai-Organization", headers)
        self.assertNotIn("Cookie", headers)
        from packet_inspector.analyzer import decode_body
        self.assertEqual(decode_body(body, headers.get("Content-Encoding", "identity")), expected)
        self.assertEqual(self.owner.store.list(), [])
        packets = self.guest.store.list()
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["upstream_usage"]["input_tokens"], 37)
        self.assertEqual(self.owner.sharing.status()["host"]["requests"], 1)
        for path in self.root.rglob('*'):
            if path.is_file():
                stored = path.read_bytes()
                self.assertNotIn(b'dummy-owner-secret', stored, path.name)
                self.assertNotIn(parse_link(link)[2].encode(), stored, path.name)

    def test_routes_are_restricted_and_replacing_link_revokes_previous_grant(self):
        link = self.start()
        self.assertEqual(self.remote_request(link)[0], 200)
        self.assertEqual(self.remote_request(link, grant=False)[0], 403)
        for path in ("/api/status", "/api/settings", "/v1/responses/other", "/v1/responses?url=https://example.invalid"):
            self.assertEqual(self.remote_request(link, path)[0], 404)
        self.assertEqual(self.remote_request(link, "/v1/responses")[0], 426)
        fresh = self.owner.sharing.rotate()["link"]
        self.assertEqual(self.remote_request(link)[0], 403)
        self.assertEqual(self.remote_request(fresh)[0], 200)
        self.assertEqual(self.authorization.calls, 1)
        self.assertFalse(self.received)
        state = json.dumps(self.owner.sharing.status())
        self.assertNotIn("dummy-owner", state)
        self.assertNotIn(parse_link(fresh)[2], state)

    def test_certificate_mismatch_sends_no_grant_and_does_not_change_config(self):
        link = self.start()
        original = self.guest.link.config.read_bytes()
        bad = link.rsplit(".", 1)[0] + "." + "0" * 64
        with patch.object(self.owner.sharing, "authorized", wraps=self.owner.sharing.authorized) as auth:
            with self.assertRaisesRegex(SharingError, "证书不匹配"):
                self.guest.sharing.connect(bad)
            self.assertEqual(auth.call_count, 0)
        self.assertEqual(self.guest.link.config.read_bytes(), original)
        self.assertIsNone(self.guest.sharing.remote)

    def test_older_owner_protocol_is_rejected_before_changing_guest_configuration(self):
        link = self.start()
        original = self.guest.link.config.read_bytes()
        with patch('packet_inspector.sharing.pinned_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"protocol":1,"available":true}')
            with self.assertRaisesRegex(SharingError, '协议不兼容'):
                self.guest.sharing.connect(link)
        self.assertEqual(self.guest.link.config.read_bytes(), original)
        self.assertFalse(self.guest.sharing.provider.path.exists())
        self.assertIsNone(self.guest.sharing.remote)

    def test_native_model_catalog_is_not_counted_as_generation(self):
        link = self.start()
        status, body = self.remote_request(link, '/v1/models?client_version=test')
        self.assertEqual(status, 200)
        self.assertIn(b'dummy-test-model', body)
        self.assertEqual(self.owner.sharing.status()['host']['requests'], 0)

    def test_guest_config_restores_and_never_contains_grants_or_owner_credentials(self):
        link = self.start()
        original = tomllib.loads(self.guest.link.config.read_text())
        self.guest.sharing.connect(link)
        self.assertTrue(self.guest.connection_profile()["active"])
        for path in (self.guest.link.config, self.guest.sharing.provider.path):
            content = path.read_text()
            self.assertNotIn(parse_link(link)[2], content)
            self.assertNotIn("dummy-owner", content)
        self.guest.sharing.disconnect()
        self.assertEqual(tomllib.loads(self.guest.link.config.read_text()), original)
        self.assertIsNone(self.guest.sharing.remote)

    def test_guest_retry_updates_do_not_capture_or_change_original_providers(self):
        self.guest.link.config.write_text('model_provider="original"\n[model_providers.original]\nbase_url="https://original.invalid/v1"\n')
        self.guest.settings.update(capacity_retry_enabled=True, capacity_retry_max_retries=10,
                                   auto_detect_providers=False)
        self.guest.sharing.connect(self.start())
        document = tomllib.loads(self.guest.link.config.read_text())
        self.assertEqual(document["model_providers"][SHARED_PROVIDER]["stream_max_retries"], 10)
        self.guest.settings["capacity_retry_max_retries"] = 25
        self.guest.sync_providers()
        document = tomllib.loads(self.guest.link.config.read_text())
        self.assertEqual(document["model_providers"][SHARED_PROVIDER]["stream_max_retries"], 25)
        self.assertEqual(document["model_providers"]["original"], {"base_url": "https://original.invalid/v1"})
        self.guest.sharing.disconnect()
        document = tomllib.loads(self.guest.link.config.read_text())
        self.assertNotIn(SHARED_PROVIDER, document["model_providers"])
        self.assertEqual(document["model_provider"], "original")

    def test_guest_processes_packet_once_and_host_preserves_bytes_and_compact_route(self):
        link = self.start()
        self.guest.sharing.connect(link)
        self.owner.settings.update(packet_size_protection_enabled=True, budget_bytes=120000,
                                   image_isolation_enabled=True, zstd_level=22)
        self.guest.settings.update(auto_detect_providers=False, zstd_level=1)
        with patch('packet_inspector.server.prepare_request_info', wraps=__import__(
                'packet_inspector.transport', fromlist=['prepare_request_info']).prepare_request_info) as prepare:
            for path in ('/responses', '/responses/compact'):
                client, response, expected = self.request_guest(path, content='dummy prompt ' * 400)
                try:
                    self.assertEqual(response.status, 200)
                    response.read()
                finally:
                    client.close()
                wait_for_idle(self.owner, self.guest)
                self.assertEqual(self.received[-1][0], path)
                headers = {k.lower(): v for k, v in self.received[-1][1].items()}
                encoding = headers.get('content-encoding', 'identity')
                self.assertEqual(encoding, 'zstd' if path == '/responses' else 'identity')
                from packet_inspector.analyzer import decode_body
                self.assertEqual(decode_body(self.received[-1][2], encoding), expected)
        modes = [call.args[4] for call in prepare.call_args_list]
        self.assertEqual(modes, ['zstd', 'zstd'])

    def test_host_usage_is_true_upstream_usage_before_guest_predictive_rewrite(self):
        self.completion = self.completion.replace(b'"status":"completed"', b'"status":"completed","model":"gpt-6.1-sol"')
        self.guest.sharing.connect(self.start())
        self.guest.settings.update(packet_size_protection_enabled=True, budget_bytes=50001)
        client, response, _ = self.request_guest()
        try:
            result = response.read()
            self.assertIn(b'500000', result)
        finally:
            client.close()
        wait_for_idle(self.guest)
        usage = self.owner.sharing.status()['host']['usage']
        self.assertEqual(usage['input_tokens'], 37)
        self.assertEqual(usage['output_tokens'], 2)
        self.assertEqual(usage['total_tokens'], 39)
        self.assertEqual(usage['responses'], 1)
        self.assertEqual(self.guest.store.list()[0]['reported_usage']['input_tokens'], 500000)
        credential = next(row for row in self.owner.sharing.status()['credentials'] if row['direction'] == 'out')
        self.assertEqual(credential['totals']['cost_nanos'], 94_000)

    def test_shared_privacy_bypasses_host_capture_diagnostics_and_error_body_storage(self):
        self.guest.sharing.connect(self.start())
        self.response_status = 400
        self.direct_response = b'{"error":{"message":"private-upstream-error-sentinel"}}'
        self.owner.settings.update(capture_enabled=True, image_isolation_enabled=True,
                                   packet_size_protection_enabled=True, capacity_retry_enabled=True)
        with (patch.object(self.owner.store, 'add', side_effect=AssertionError('A captured B')),
              patch.object(self.owner.retry_journal, 'begin', side_effect=AssertionError('A diagnosed B')),
              patch.object(self.owner.network_diagnostics, 'request', side_effect=AssertionError('A traced B'))):
            client, response, _ = self.request_guest(headers={'session_id': 'private-chat-sentinel'},
                content='private-prompt-sentinel')
            try:
                self.assertEqual(response.status, 400)
                self.assertEqual(response.read(), self.direct_response)
            finally:
                client.close()
        wait_for_idle(self.guest)
        self.assertEqual(self.owner.store.list(), [])
        for path in self.owner.data_dir.rglob('*'):
            if path.is_file():
                stored = path.read_bytes()
                for sentinel in (b'private-upstream-error-sentinel', b'private-chat-sentinel', b'private-prompt-sentinel'):
                    self.assertNotIn(sentinel, stored, path.name)
        deadline = time.monotonic() + 3
        while not self.owner.sharing.status()['host']['usage']['missing_responses'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.owner.sharing.status()['host']['usage']['missing_responses'], 1)

    def test_compact_spend_counts_toward_dollar_budget_and_tokens_do_not_limit_requests(self):
        link = self.start(budget='1')
        self.direct_response = json.dumps({'model': 'gpt-6.1-sol', 'usage': {
            'input_tokens': 100000, 'output_tokens': 10000, 'total_tokens': 110000,
            'input_tokens_details': {'cached_tokens': 50000}}}).encode()
        for _ in range(5):
            self.assertEqual(self.remote_request(link, '/v1/responses/compact', b'{}')[0], 200)
        state = self.owner.sharing.status()['host']
        self.assertNotIn('token_limit', state)
        self.assertEqual(state['usage']['total_tokens'], 550000)
        self.assertEqual(state['usage']['input_tokens'], 500000)
        self.assertEqual(state['usage']['cached_input_tokens'], 250000)
        credential = next(row for row in self.owner.sharing.status()['credentials'] if row['direction'] == 'out')
        self.assertEqual(credential['budget_nanos'], 1_000_000_000)
        self.assertEqual(credential['period_cost_nanos'], 1_025_000_000)
        self.assertEqual(self.remote_request(link, '/v1/responses', b'{}')[0], 403)
        self.assertEqual(self.remote_request(link, '/v1/models')[0], 200)
        self.assertEqual(len(self.received), 5)
        self.owner.sharing.stop_host()
        self.assertEqual(self.owner.sharing.status()['host']['usage']['total_tokens'], 550000)
        self.authorization = DummyAuthorization()
        self.start()
        self.assertEqual(self.owner.sharing.status()['host']['usage']['total_tokens'], 0)

    def test_upstream_retry_after_reaches_guest_and_host_does_not_retry(self):
        self.guest.sharing.connect(self.start())
        self.response_status = 429
        self.extra_response_headers = {'Retry-After': '196'}
        self.direct_response = b'{"error":{"message":"Selected model is at capacity. Please try a different model."}}'
        self.owner.settings.update(capacity_retry_enabled=True, capacity_retry_delay_seconds=99)
        self.guest.settings.update(capacity_retry_enabled=True, capacity_retry_delay_seconds=30)
        client, response, _ = self.request_guest()
        try:
            self.assertEqual(response.status, 429)
            self.assertEqual(response.getheader('Retry-After'), '196')
            self.assertIn(b'Retry-After', response.read())
        finally:
            client.close()
        wait_for_idle(self.guest)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.owner.sharing.status()['host']['requests'], 1)

    def test_late_completion_does_not_pollute_a_new_sharing_session(self):
        self.guest.sharing.connect(self.start())
        self.gate_stream = True
        client, response, _ = self.request_guest()
        try:
            first = response.read1(65536)
            self.assertIn(b'"delta":"first"', first)
            prior = self.owner.sharing.host
            self.owner.sharing.stop_host()
            self.authorization = DummyAuthorization()
            self.start()
            self.release_stream.set()
            self.assertTrue((first + response.read()).endswith(self.completion))
        finally:
            self.release_stream.set()
            client.close()
        wait_for_idle(self.guest)
        self.assertEqual(prior['usage']['total_tokens'], 39)
        current = self.owner.sharing.status()['host']
        self.assertEqual(current['usage']['total_tokens'], 0)
        self.assertEqual(current['requests'], 0)

    def test_dollar_budget_validation_occurs_before_native_authorization(self):
        for value in (-1, 0, 0.99, True, 1_000_001, 'NaN', 'invalid', None):
            with self.subTest(value=value), self.assertRaises(SharingError):
                self.start(budget=value)
        with self.assertRaises(SharingError):
            self.owner.sharing.issue(expires_at=time.time() + 3600)
        self.assertEqual(self.authorization.calls, 0)

    def test_original_start_api_uses_dollars_and_rejects_the_retired_token_limit(self):
        action = {'action': 'host_start', 'address': '127.0.0.1', 'port': free_port()}
        status, result = self.local_api('/api/sharing', {**action, 'token_limit': 100})
        self.assertEqual(status, 400)
        self.assertIn('美元', result['error'])
        self.assertEqual(self.authorization.calls, 0)
        self.assertIsNone(self.owner.sharing.listener)
        with patch.object(self.owner.sharing, '_host_upstream', return_value=self.target):
            status, result = self.local_api('/api/sharing', {**action, 'budget': '1.50'})
        self.assertEqual(status, 200)
        self.assertEqual(result['credentials'][0]['budget_nanos'], 1_500_000_000)
        self.assertNotIn('token_limit', result['host'])

    def test_lending_and_borrowing_are_mutually_exclusive_across_instances_on_one_machine(self):
        self.guest.sharing.role = MachineSharingRole(self.owner.sharing.role.name)
        original = self.guest.link.config.read_bytes()
        link = self.start()
        with self.assertRaisesRegex(SharingError, '本机已有实例'):
            self.guest.sharing.connect(link)
        self.assertEqual(self.guest.link.config.read_bytes(), original)
        self.assertEqual(self.authorization.calls, 1)
        self.owner.sharing.stop_host()
        self.guest.sharing.role.acquire()  # Emulate B already borrowing on this same machine.
        try:
            with self.assertRaisesRegex(SharingError, '本机已有实例'):
                self.start()
            self.assertIsNone(self.owner.sharing.listener)
            self.assertEqual(self.authorization.calls, 1)
        finally:
            self.guest.sharing.role.close()

    def test_role_is_released_after_failed_start_and_failed_connection(self):
        with patch.object(self.owner.sharing, '_host_upstream', side_effect=SharingError('dummy failure')):
            with self.assertRaises(SharingError):
                self.owner.sharing.start_host('127.0.0.1', free_port())
        contender = MachineSharingRole(self.owner.sharing.role.name)
        try:
            contender.acquire()
        finally:
            contender.close()
        link = self.start()
        original = self.guest.link.config.read_bytes()
        bad = link.rsplit('.', 1)[0] + '.' + '0' * 64
        with self.assertRaises(SharingError):
            self.guest.sharing.connect(bad)
        contender = MachineSharingRole(self.guest.sharing.role.name)
        try:
            contender.acquire()
        finally:
            contender.close()
        self.assertEqual(self.guest.link.config.read_bytes(), original)

    def test_stop_rejects_new_requests_without_truncating_started_stream(self):
        self.guest.sharing.connect(self.start())
        self.gate_stream = True
        client, response, _ = self.request_guest()
        try:
            first = response.read1(65536)
            self.assertIn(b'"delta":"first"', first)
            previous = self.owner.sharing.host
            self.owner.sharing.stop_host()
            self.release_stream.set()
            self.assertTrue((first + response.read()).endswith(self.completion))
            wait_for_idle(self.owner, self.guest)
            deadline = time.monotonic() + 3
            while previous['in_flight'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(previous['in_flight'], 0)
        finally:
            self.release_stream.set()
            client.close()

    def test_new_instance_recovers_provider_after_an_unclean_exit(self):
        self.guest.sharing.connect(self.start())
        original_config = self.guest.link.config
        # Emulate a crash by leaving the recovery record/config but losing memory.
        self.guest.sharing.remote = None
        reopened = InspectorServer(('127.0.0.1', 0), self.guest.data_dir, original_config, self.target)
        try:
            self.assertNotEqual(tomllib.loads(original_config.read_text()).get('model_provider'), SHARED_PROVIDER)
            self.assertFalse(reopened.sharing.provider.path.exists())
            self.assertTrue(reopened.connection_reload_required)
            self.assertTrue(reopened.sharing.status()['recovered'])
        finally:
            reopened.server_close()

    def test_invalid_optional_recovery_metadata_cannot_break_ordinary_relay_lifecycle(self):
        root = self.root / 'invalid-recovery'
        root.mkdir()
        config = root / 'config.toml'
        config.write_text('model="test"\n')
        data = root / 'data'
        data.mkdir()
        (data / 'sharing-provider.json').write_text('[]')
        relay = InspectorServer(('127.0.0.1', 0), data, config, self.target)
        try:
            self.assertTrue(relay.sharing.status()['recovery_error'])
            self.assertIn('version', relay.status())
        finally:
            relay.server_close()

    def test_unlimited_requests_still_enforce_expiration_and_native_account_change(self):
        link = self.start()
        self.assertEqual(self.remote_request(link, "/v1/responses", b'{"model":"test","input":[]}')[0], 200)
        self.assertEqual(self.remote_request(link, "/v1/responses", b'{}')[0], 200)
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.owner.sharing.status()["host"]["requests"], 2)
        self.owner.sharing.host["expires_at"] = time.time() - 1
        self.assertEqual(self.remote_request(link)[0], 403)
        self.assertFalse(self.owner.sharing.status()["host"]["active"])
        self.owner.sharing.stop_host()
        self.authorization = DummyAuthorization()
        link = self.start()
        self.authorization.identity = "another-account"
        self.assertEqual(self.remote_request(link, "/v1/responses", b'{}')[0], 403)
        self.assertFalse(self.owner.sharing.status()["host"]["active"])
        self.assertEqual(len(self.received), 2)

    def test_custom_upstream_is_not_sent_native_credentials(self):
        self.owner.link.config.write_text('model_provider="custom"\n[model_providers.custom]\nbase_url="https://example.invalid/v1"\n')
        with self.assertRaisesRegex(SharingError, "不是 Codex 原生登录出口"):
            self.owner.sharing.start_host("127.0.0.1", free_port())
        self.assertTrue(self.authorization.closed)
        self.assertFalse(self.received)

    def test_size_protection_checkpoint_uses_pinned_shared_connection(self):
        from tests.test_overflow_compaction import FakeModel, make_body, message
        self.upstream.RequestHandlerClass = FakeModel
        self.upstream.requests, self.upstream.fail = [], False
        self.guest.sharing.connect(self.start())
        foreign = "codex-exit://127.0.0.1:47847/#" + secrets.token_urlsafe(32) + "." + "b" * 64
        class Selected:
            def close(self):
                pass
        self.guest.egress.opener_factory = lambda *_args, **_kwargs: Selected()
        self.guest.egress.select("peer", foreign)
        routes = []
        def delegated(link, **_kwargs):
            routes.append(link)
            # A setting changed during preparation must not split this request's
            # summaries and generation across different network exits.
            self.guest.egress.select("provider")
            return self.owner.opener
        self.owner.egress.opener_factory = delegated
        self.guest.settings.update(packet_size_protection_enabled=True, budget_bytes=4000, zstd_level=3)
        client = http.client.HTTPConnection('127.0.0.1', self.guest.port, timeout=HTTP_TIMEOUT)
        try:
            with patch.object(self.guest.opener, 'open', side_effect=AssertionError('Shared compaction bypassed certificate pinning')):
                raw = make_body([message(i) for i in range(30)])
                client.request('POST', urlsplit(self.guest.sharing.client_endpoint).path + '/responses', raw,
                    {'Content-Type': 'application/json', 'session_id': 'dummy-shared-overflow'})
                response = client.getresponse()
                result = response.read()
                self.assertEqual(response.status, 200, result)
        finally:
            client.close()
        wait_for_idle(self.owner, self.guest)
        self.assertGreaterEqual(len(self.upstream.requests), 2)
        self.assertTrue(any(compact for _, compact, _ in self.upstream.requests))
        self.assertTrue(all(size <= 4000 for size, _, _ in self.upstream.requests))
        self.assertEqual(routes, [foreign] * len(self.upstream.requests))

    def test_control_api_validates_origin_and_returns_no_grant_in_status(self):
        def call(method, data=None, origin=None):
            client = http.client.HTTPConnection('127.0.0.1', self.owner.port, timeout=HTTP_TIMEOUT)
            try:
                headers = {'Content-Type': 'application/json'}
                if origin:
                    headers['Origin'] = origin
                client.request(method, '/api/sharing', json.dumps(data).encode() if data else None, headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        action = {'action': 'host_start', 'address': '127.0.0.1', 'port': free_port()}
        self.assertEqual(call('POST', action, 'https://example.invalid')[0], 403)
        self.assertEqual(self.authorization.calls, 0)
        with patch.object(self.owner.sharing, '_host_upstream', return_value=self.target):
            status, result = call('POST', action)
        self.assertEqual(status, 200)
        self.assertIn('link', result)
        status, state = call('GET')
        self.assertEqual(status, 200)
        self.assertNotIn(parse_link(result['link'])[2], json.dumps(state))
        self.assertEqual(call('POST', {'action': 'host_stop'})[0], 200)

    def test_disconnecting_shared_source_keeps_normal_local_relay_working(self):
        link = self.start()
        self.guest.sharing.connect(link)
        self.guest.set_connection(False)
        self.assertIsNone(self.guest.sharing.remote)
        client = http.client.HTTPConnection("127.0.0.1", self.guest.port, timeout=HTTP_TIMEOUT)
        try:
            client.request("POST", urlsplit(self.guest.endpoint).path + "/responses", b'{}',
                           {"Authorization": "Bearer dummy-local-authorization"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            client.close()
        wait_for_idle(self.guest)
        self.assertEqual(self.received[-1][1]["Authorization"], "Bearer dummy-local-authorization")


class LinkTests(unittest.TestCase):
    def test_automatic_address_prefers_the_routed_interface(self):
        with patch('packet_inspector.sharing.socket.socket') as factory:
            route = factory.return_value.__enter__.return_value
            route.getsockname.return_value = ('192.168.3.20', 12345)
            self.assertEqual(automatic_address(), '192.168.3.20')
            route.send.assert_not_called()
            route.sendto.assert_not_called()

    def test_automatic_address_falls_back_to_lan_and_rejects_loopback_only(self):
        with patch('packet_inspector.sharing.socket.socket', side_effect=OSError()), \
             patch('packet_inspector.sharing.local_addresses', return_value=['10.1.2.3']):
            self.assertEqual(automatic_address(), '10.1.2.3')
        with patch('packet_inspector.sharing.socket.socket', side_effect=OSError()), \
             patch('packet_inspector.sharing.local_addresses', return_value=['127.0.0.1']):
            with self.assertRaisesRegex(SharingError, '局域网'):
                automatic_address()

    def test_no_public_urls_path_traversal_queries_or_extra_credentials(self):
        fragment = secrets.token_urlsafe(32) + "." + "f" * 64
        for value in (f"https://192.168.1.2:47846/#{fragment}", f"codex-share://8.8.8.8:443/#{fragment}",
                      f"codex-share://user@192.168.1.2:47846/#{fragment}", f"codex-share://192.168.1.2:47846/../../api/#{fragment}",
                      f"codex-share://192.168.1.2:47846/?key=foo#{fragment}"):
            with self.subTest(value=value), self.assertRaises(SharingError):
                parse_link(value)

    def test_forwarding_drops_identity_hop_and_proxy_headers(self):
        result = forwarding_headers({"Authorization": "dummy", "ChatGPT-Account-Id": "dummy", "Cookie": "dummy",
            "Connection": "x-private", "X-Private": "dummy", "X-Forwarded-For": "dummy", "X-Inspector-Control": "dummy",
            "Content-Type": "application/json", "Session_id": "dummy-session"})
        self.assertEqual(result, {"Content-Type": "application/json", "Session_id": "dummy-session"})


if __name__ == "__main__":
    unittest.main()
