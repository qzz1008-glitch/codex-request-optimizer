"""Private proxy configuration, live switching and actual outbound routing."""
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import time
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener

from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.proxy_settings import (
    SETTINGS_KEY, ProxySettings, SelectedProxyHandler, bypass_proxy, proxy_address, system_proxies,
)
from packet_inspector.proxy_test import ProxyTester
from packet_inspector.server import NoRedirect
from packet_inspector.shared_egress import physical_tunnel
from packet_inspector.sharing import tls_context
from packet_inspector.sharing_credentials import CredentialVault
from tests import test_outbound_refresh as fixtures


class ProxySettingsBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser contracts")
    def test_proxy_settings_save_restore_and_detection(self):
        result = subprocess.run([shutil.which("node"), "tests/proxy_settings.cjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf8", timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        if "skipped" in summary:
            self.skipTest(summary["skipped"])
        self.assertEqual(summary["proxy_settings"], "passed")
        self.assertGreaterEqual(summary["browser_cases"], 11)


class ProxySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = CredentialVault(self.root)
        self.proxies = {"https": "http://auto-user:auto-secret@proxy.invalid:8080", "no": "*.internal"}
        self.config = ProxySettings(self.vault, lambda: dict(self.proxies))

    def test_detection_displays_only_the_address_and_preserves_the_automatic_route(self):
        with patch("urllib.request.getproxies_environment", return_value=self.proxies):
            status = self.config.status()
        self.assertEqual(status["mode"], "auto")
        self.assertEqual(status["detected"]["source"], "environment")
        self.assertEqual(status["detected"]["https"], {"address": "http://proxy.invalid:8080"})
        self.assertNotIn("auto-user", json.dumps(status))
        self.assertNotIn("auto-secret", json.dumps(status))
        self.assertEqual(self.config.selection(), (self.proxies, "auto"))
        self.assertFalse((self.root / "proxy-settings.key").exists())

    def save_manual(self):
        return self.config.configure({"mode": "manual", "address": "proxy.invalid:8080",
                                      "bypass": "internal.test;*.lan"})

    def test_legacy_fields_are_ignored_on_read_and_cleared_on_save(self):
        address, bypass = "http://proxy.invalid:8080", "internal.test;*.lan"
        manual = {"address": address, "bypass": bypass}
        # A legacy credential whose encryption key is unavailable on this host.
        legacy = {"mode": "manual", "manual": {**manual, "username": "old-user", "password": "dW5yZWFkYWJsZQ=="}}
        with self.vault.connection() as db:
            db.execute("INSERT INTO meta(key,value) VALUES (?,?)", (SETTINGS_KEY, json.dumps(legacy)))
        reopened = ProxySettings(self.vault, lambda: self.proxies)
        self.assertEqual(reopened.status()["manual"], manual)
        self.assertEqual(reopened.selection(), ({"http": address, "https": address, "no": bypass}, "manual"))
        with self.vault.connection() as db:
            stored = db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone()[0]
        self.assertEqual(json.loads(stored), legacy)
        reopened.configure({"mode": "manual", "address": "new.invalid:8080"})
        with self.vault.connection() as db:
            stored = db.execute("SELECT value FROM meta WHERE key=?", (SETTINGS_KEY,)).fetchone()[0]
        self.assertEqual(json.loads(stored)["manual"], {
            "address": "http://new.invalid:8080", "bypass": bypass, "username": "", "password": "",
        })
        self.assertFalse((self.root / "proxy-settings.key").exists())

    def test_automatic_mode_retains_manual_fields_but_uses_current_system_proxy(self):
        self.save_manual()
        self.config.configure({"mode": "auto"})
        self.proxies = {"https": "http://regional.invalid:8080"}
        self.assertEqual(self.config.selection(), (self.proxies, "auto"))
        self.config.configure({"mode": "manual"})
        self.assertEqual(self.config.status()["manual"], {
            "address": "http://proxy.invalid:8080", "bypass": "internal.test;*.lan",
        })

    def test_unsaved_proxy_preview_does_not_change_settings(self):
        before = self.vault.path.read_bytes()
        selected, mode = self.config.preview({"mode": "manual", "address": "preview.invalid:8080", "bypass": ""})
        self.assertEqual(mode, "manual")
        self.assertEqual(selected["https"], "http://preview.invalid:8080")
        self.assertEqual(self.vault.path.read_bytes(), before)
        self.assertEqual(self.config.read()["mode"], "auto")
        self.assertFalse((self.root / "proxy-settings.key").exists())

    def test_invalid_changes_are_atomic_and_do_not_echo_secrets(self):
        self.save_manual()
        before = self.config.read()
        for fields in ({"address": "http://user:leak-me@proxy.invalid"}, {"address": "socks5://proxy.invalid"},
                       {"address": "http://proxy.invalid/path"}, {"address": "http://proxy.invalid:65536"},
                       {"username": "leak-me"}, {"password": "leak-me"}, {"bypass": 1}):
            with self.subTest(fields=tuple(fields)):
                with self.assertRaises(ValueError) as error:
                    self.config.configure({"mode": "manual", **fields})
                self.assertNotIn("leak-me", str(error.exception))
                self.assertEqual(self.config.read(), before)
        self.assertEqual(proxy_address("https://[2001:db8::1]:8443/"), "https://[2001:db8::1]:8443")

    def test_auto_detection_reports_unsupported_pac_and_all_proxy(self):
        self.proxies = {"all": "socks5://proxy.invalid:1080"}
        self.config.system_details = lambda: {"windows_pac_url": "https://proxy.invalid/proxy.pac"}
        with patch("urllib.request.getproxies_environment", return_value=self.proxies):
            status = self.config.status()["detected"]
        self.assertEqual(status["source"], "direct")
        self.assertTrue(status["unsupported_all_proxy"])
        self.assertEqual(status["pac_url"], "https://proxy.invalid/proxy.pac")

    def test_windows_auto_mode_snapshots_registry_exclusions_without_overriding_environment(self):
        proxy = {"https": "http://proxy.invalid:8080"}
        with patch("packet_inspector.proxy_settings.os.name", "nt"), \
                patch("urllib.request.getproxies_registry", return_value=proxy, create=True), \
                patch("urllib.request.getproxies_environment", return_value={}) as environment, \
                patch("packet_inspector.proxy_settings._windows_bypass", return_value="<local>;*.internal") as exclusions:
            detected = system_proxies()
            self.assertTrue(bypass_proxy("machine.internal:443", detected))
            self.assertTrue(bypass_proxy("intranet:443", detected))
            exclusions.assert_called_once()
            environment.return_value = {"https": proxy["https"]}
            self.assertNotIn("no", system_proxies())
            exclusions.assert_called_once()
            environment.return_value = {"no": "*.separate", "node_use_env": "1"}
            self.assertEqual(system_proxies(), {**proxy, "no": "*.separate"})
            exclusions.assert_called_once()

    def test_missing_scheme_uses_windows_proxy_and_its_own_exclusions(self):
        windows = {"http": "http://windows.invalid:8080", "https": "http://windows.invalid:8080"}
        environment = {"http": "http://process.invalid:8080"}
        with patch("packet_inspector.proxy_settings.os.name", "nt"), \
                patch("urllib.request.getproxies_registry", return_value=windows, create=True), \
                patch("packet_inspector.proxy_settings._windows_bypass", return_value="*.internal"):
            detected = system_proxies(environment)
            with_explicit_rules = system_proxies({**environment, "no": ".separate"})
        self.assertEqual(detected["http"], environment["http"])
        self.assertEqual(detected["https"], windows["https"])
        for scheme in ("http", "https"):
            self.assertEqual(bypass_proxy("site.internal", detected, scheme), scheme == "https")
            self.assertFalse(bypass_proxy("site.internal", with_explicit_rules, scheme))
            self.assertTrue(bypass_proxy("site.separate", with_explicit_rules, scheme))

    def test_bypass_uses_selected_rules_including_windows_forms(self):
        for host in ("127.0.0.1:47844", "[::1]:47844", "localhost:47844", "intranet", "a.internal",
                     "api.example.com:443", "single.lan", "[2001:db8::2]:443"):
            with self.subTest(host=host):
                self.assertTrue(bypass_proxy(host, {"no": "<local>;*.internal,.example.com:443;single.lan;2001:db8::2"}))
        self.assertFalse(bypass_proxy("api.example.com:80", {"no": ".example.com:443"}))
        self.assertFalse(bypass_proxy("external.test", {"no": ""}))


class ProxyRoutingTests(unittest.TestCase):
    setUp = fixtures.OutboundRefreshTests.setUp
    select = fixtures.OutboundRefreshTests.select
    close_gateways = fixtures.OutboundRefreshTests.close_gateways

    def test_auto_uses_system_gateway_for_requests_with_only_the_node_proxy_switch(self):
        gateway = self.select(0)
        self.relay.proxy_config.auto_proxies = system_proxies
        with patch.dict(os.environ, {"NODE_USE_ENV_PROXY": "1", "NO_PROXY": "127.0.0.1"}, clear=True), \
                patch("urllib.request.getproxies_registry", return_value=self.proxies, create=True):
            opener = self.relay.outgoing_opener()
            with opener.open(Request("http://upstream.invalid/responses", data=b"trial"), timeout=3) as response:
                self.assertEqual(response.read(), b"aa")
            physical_tunnel(self.relay, "upstream.invalid", 443).close()
        self.assertEqual(opener.inspector_proxies["https"], gateway)
        self.assertEqual(self.calls, [("a", "http://upstream.invalid/responses", b"trial"),
                                      ("a", "CONNECT", "upstream.invalid:443")])

    def test_manual_configuration_overrides_process_no_proxy_and_keeps_inflight_response(self):
        auto = self.select(0)
        manual = self.select(1)
        self.proxies = {"http": auto, "https": auto, "no": "*"}
        self.relay.proxy_config.configure({"mode": "manual", "address": manual, "bypass": ""})
        with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            old = self.relay.outgoing_opener()
            req = Request("http://upstream.invalid/responses", data=b"old", headers={"X-Test-Hold": "1"})
            with old.open(req, timeout=3) as response:
                self.assertEqual(response.read(1), b"b")
                self.relay.proxy_config.configure({"mode": "auto"})
                self.proxies["no"] = ""
                with self.relay.outgoing_opener().open(Request(req.full_url, data=b"new"), timeout=3) as newer:
                    self.assertEqual(newer.read(), b"aa")
                self.release.set()
                self.assertEqual(response.read(), b"b")
        self.assertEqual([(r[0], r[2]) for r in self.calls], [("b", b"old"), ("a", b"new")])
        self.assertEqual(old.inspector_proxy_mode, "manual")

    def test_manual_bypass_and_physical_exit_follow_saved_settings(self):
        address = self.select(0)
        self.relay.proxy_config.configure({"mode": "manual", "address": address, "bypass": "upstream.invalid"})
        with patch("socket.create_connection", side_effect=OSError("selected-direct")) as direct:
            with self.assertRaises(OSError):
                self.relay.outgoing_opener().open(Request("http://upstream.invalid/responses", data=b"bypassed"), timeout=1)
            self.assertEqual(direct.call_args.args[0], ("upstream.invalid", 80))
        self.assertEqual(self.calls, [])
        physical_tunnel(self.relay, "api.openai.com", 443).close()
        self.assertEqual(self.calls, [("a", "CONNECT", "api.openai.com:443")])

    def test_api_saves_and_tests_an_address_without_a_request_record_or_restart(self):
        threading.Thread(target=self.relay.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(self.relay.shutdown)
        ui = DesktopUI(self.relay.port).start()
        self.addCleanup(ui.close)
        address = self.select(0)
        data = {"mode": "manual", "address": address}

        def call(method, payload=None, path="/api/proxy-settings"):
            connection = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=3)
            self.addCleanup(connection.close)
            connection.request(method, path, json.dumps(payload) if payload is not None else None)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        code, status = call("POST", data)
        self.assertEqual(code, 200)
        self.assertEqual(status["effective"]["https"]["address"], address)
        self.assertEqual(call("GET")[1], status)
        for path in ("/api/proxy-settings", "/api/proxy-test"):
            for fields in ({"username": "private-user"}, {"password": "private-secret"},
                           {"address": "http://u:private-secret@proxy.invalid"}):
                code, result = call("POST", {**data, **fields}, path)
                self.assertEqual(code, 400)
                self.assertNotIn("private-", json.dumps(result))
        self.assertEqual(self.relay.store.list(limit=1), [])
        self.assertEqual(self.relay.outgoing_opener().inspector_proxy_mode, "manual")
        preview_address = self.select(1)
        with patch.object(self.relay.proxy_tester, "test", return_value={"state": "ok", "status": 401}) as test:
            self.assertEqual(call("POST", {**data, "address": preview_address}, "/api/proxy-test"),
                             (200, {"state": "ok", "status": 401}))
            self.assertEqual(test.call_args.args[0]["https"], preview_address)
        self.assertEqual(call("GET")[1]["manual"]["address"], address)

    def test_manual_error_diagnostics_keep_route_snapshot(self):
        address = self.select(0)
        self.relay.proxy_config.configure({"mode": "manual", "address": address})
        selected = self.relay.outgoing_opener()
        journal = self.relay.network_diagnostics
        trace = journal.request("https://upstream.invalid/responses", "POST", {}, b"{}", opener=selected)
        self.relay.proxy_config.configure({"mode": "auto"})
        self.relay.outgoing_opener()
        with patch.object(journal, "_save") as save:
            trace.finish(ConnectionRefusedError("connection refused"))
        record = save.call_args.args[0]
        self.assertEqual(record["environment"]["proxy_mode"], "manual")
        self.assertEqual(record["environment"]["proxy_configuration_source_now"], "manual")


class ProxyAuthenticationTests(unittest.TestCase):
    def test_http_and_tls_proxy_authentication_and_error_responses(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        vault = CredentialVault(Path(temporary.name))
        context, _ = tls_context("127.0.0.1", vault, purpose="proxy-test")
        identity = json.loads(vault.secret("tls:proxy-test:127.0.0.1", lambda: b""))
        trust = ssl.create_default_context(cadata=identity["cert"])
        observed = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                observed.append((self.path, dict(self.headers)))
                self.send_response(403 if self.path.endswith("error") else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
        for scheme in ("http", "https"):
            with self.subTest(proxy=scheme):
                gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
                gateway.daemon_threads = True
                if scheme == "https":
                    gateway.socket = context.wrap_socket(gateway.socket, server_side=True)
                threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
                self.addCleanup(gateway.server_close)
                self.addCleanup(gateway.shutdown)
                address = f"{scheme}://name:p%40ss@127.0.0.1:{gateway.server_port}"
                opener = build_opener(SelectedProxyHandler({"http": address}, trust), NoRedirect())
                with opener.open(Request("http://upstream.invalid/ok", data=b"body"), timeout=3) as response:
                    self.assertEqual(response.read(), b"ok")
                self.assertEqual(observed[-1][1]["Proxy-Authorization"],
                    "Basic " + base64.b64encode(b"name:p@ss").decode())
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request("http://upstream.invalid/error", data=b"body"), timeout=3)
                with error.exception as response:
                    self.assertEqual(response.code, 403)
                    self.assertEqual(response.read(), b"ok")

    def test_https_origin_through_http_and_tls_connect_proxies_keeps_auth_at_proxy(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        vault = CredentialVault(Path(temporary.name))
        origin_context, _ = tls_context("127.0.0.2", vault, purpose="origin-test")
        identity = json.loads(vault.secret("tls:origin-test:127.0.0.2", lambda: b""))
        trust = ssl.create_default_context(cadata=identity["cert"])
        origins, proxies = [], []
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                origins.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            def do_HEAD(self):
                origins.append((dict(self.headers), None))
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        origin.daemon_threads = True
        origin.socket = origin_context.wrap_socket(origin.socket, server_side=True)
        threading.Thread(target=origin.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        class ConnectProxy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_CONNECT(self):
                proxies.append((self.path, self.headers.get("Proxy-Authorization")))
                with socket.create_connection(("127.0.0.1", origin.server_port), timeout=3) as target:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.flush()
                    streams = (self.connection, target)
                    while True:
                        ready, _, _ = select.select(streams, [], [], 3)
                        if not ready:
                            return
                        try:
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (target if source is self.connection else self.connection).sendall(data)
                        except ConnectionError:
                            return
        for scheme in ("http", "https"):
            with self.subTest(proxy=scheme):
                gateway = ThreadingHTTPServer(("127.0.0.2", 0), ConnectProxy)
                gateway.daemon_threads = True
                if scheme == "https":
                    gateway.socket = origin_context.wrap_socket(gateway.socket, server_side=True)
                threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
                self.addCleanup(gateway.server_close)
                self.addCleanup(gateway.shutdown)
                address = f"{scheme}://name:p%40ss@127.0.0.2:{gateway.server_port}"
                opener = build_opener(SelectedProxyHandler({"https": address}, trust),
                                     HTTPSHandler(context=trust), NoRedirect())
                with opener.open(Request("https://127.0.0.2/ok", data=b"intact"), timeout=3) as response:
                    self.assertEqual(response.read(), b"ok")
                self.assertEqual(proxies[-1], ("127.0.0.2:443", "Basic " + base64.b64encode(b"name:p@ss").decode()))
                self.assertEqual(origins[-1][1], b"intact")
                self.assertNotIn("Proxy-Authorization", origins[-1][0])
                tester = ProxyTester(lambda values: build_opener(SelectedProxyHandler(values, trust),
                    HTTPSHandler(context=trust), NoRedirect()))
                result = tester.test({"https": address}, "manual", "https://127.0.0.2/probe")
                self.assertEqual((result["state"], result["status"], result["proxy_used"]), ("ok", 401, True))
                self.assertIsNone(origins[-1][1])
                self.assertNotIn("Authorization", origins[-1][0])
                self.assertNotIn("Proxy-Authorization", origins[-1][0])


class ProxyTestTests(unittest.TestCase):
    def test_head_probe_reports_http_rejection_and_loopback_bypass_without_account_auth(self):
        observed = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_HEAD(self):
                observed.append((self.command, self.path, dict(self.headers)))
                self.send_response(int(self.path.rsplit("/", 1)[-1]))
                self.send_header("Content-Length", "0")
                self.end_headers()
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)
        tester = ProxyTester(lambda values: build_opener(SelectedProxyHandler(values), NoRedirect()))
        proxies = {"http": f"http://probe-user:probe-secret@127.0.0.1:{gateway.server_port}"}
        for status, expected in ((200,"ok"),(401,"ok"),(403,"rejected"),(407,"rejected"),(504,"rejected")):
            result = tester.test(proxies, "manual", "http://upstream.invalid/" + str(status))
            self.assertEqual((result["state"], result["status"]), (expected,status))
            self.assertTrue(result["proxy_used"])
            self.assertNotIn("probe-secret", json.dumps(result))
            self.assertNotIn("Authorization", observed[-1][2])
            self.assertEqual(observed[-1][0], "HEAD")
        self.assertEqual(len(observed), 5)
        result = tester.test(proxies, "manual", f"http://127.0.0.1:{gateway.server_port}/200")
        self.assertEqual((result["state"], result["proxy_used"], result["bypassed"]), ("ok",False,True))
        self.assertNotIn("Proxy-Authorization", observed[-1][2])

    def test_probe_deadline_and_single_slot_bound_unresponsive_resolvers(self):
        release, closed = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Opener:
            def open(self, request, timeout):
                release.wait(3)
                class Response:
                    status = 200
                    def __enter__(self): return self
                    def __exit__(self, *_args): closed.set()
                return Response()
        tester = ProxyTester(lambda values: Opener(), timeout=.05)
        started = time.monotonic()
        self.assertEqual(tester.test({},"auto","https://upstream.invalid")["state"], "timeout")
        self.assertLess(time.monotonic()-started, .5)
        with self.assertRaisesRegex(ValueError, "正在进行"):
            tester.test({},"auto","https://upstream.invalid")
        release.set()
        self.assertTrue(closed.wait(1))
        self.assertTrue(tester.running.acquire(timeout=1))
        tester.running.release()
        self.assertEqual(tester.test({},"auto","https://upstream.invalid")["state"], "ok")

    def test_proxy_test_errors_never_return_passwords_or_encoded_auth(self):
        def fail(values):
            raise OSError("failed https://probe-user:probe-secret@proxy.invalid; probe-secret")
        tester = ProxyTester(fail)
        result = tester.test({"https":"http://probe-user:probe-secret@proxy.invalid"},"manual","https://upstream.invalid")
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("probe-secret", json.dumps(result))
