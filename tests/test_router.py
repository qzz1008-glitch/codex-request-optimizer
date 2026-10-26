import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packet_inspector import VERSION
from packet_inspector.router import RouterServer, atomic_json, ensure_entry
from packet_inspector.server import InspectorServer


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n', encoding="utf8")
        self.data = self.root / "data"
        self.started, self.release = threading.Event(), threading.Event()
        self.seen = []
        test = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(handler):
                test.seen.append((handler.path, None, handler.headers.get("Authorization")))
                result = b"existing-response"
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(result)))
                handler.end_headers()
                handler.wfile.write(result)
            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                test.seen.append((handler.path, body, handler.headers.get("Authorization")))
                if handler.headers.get("X-Test-Drop"):
                    handler.close_connection = True
                    return
                streaming = bool(handler.headers.get("X-Test-Stream"))
                result = b"abcdef" if streaming else b"ok"
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(result)))
                handler.end_headers()
                handler.wfile.write(result[:1])
                handler.wfile.flush()
                if streaming:
                    test.started.set()
                    test.release.wait(5)
                handler.wfile.write(result[1:])
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.run_server(self.upstream)
        self.workers = []
        self.first = self.worker("1" * 32)
        atomic_json(self.data / "router-state.json", {"protocol": 1, "token": "router-test", "config": str(self.config),
                    "active": self.first.worker_id, "draining": [], "port": 47844})
        self.router = RouterServer(("127.0.0.1", 0), self.data, self.config)
        self.first.worker_control({"action": "activate", "public_port": self.router.port})
        self.first.save_settings({**self.first.settings, "auto_detect_providers": False})
        self.run_server(self.router)
        self.addCleanup(self.stop)

    @staticmethod
    def run_server(server):
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()

    def stop(self):
        self.release.set()
        self.router.shutdown()
        self.router.server_close()
        deadline = time.monotonic() + 10
        while any(worker.active_requests for worker in self.workers) and time.monotonic() < deadline:
            time.sleep(.01)
        for server in [*self.workers, self.upstream]:
            server.shutdown()
            server.server_close()
            if hasattr(server, "analysis_thread"):
                server.analysis_thread.join(timeout=12)

    def worker(self, identity):
        worker = InspectorServer(("127.0.0.1", 0), self.data, self.config,
                                 f"http://127.0.0.1:{self.upstream.server_port}", worker_id=identity,
                                 worker_token="worker-test", public_port=47844)
        atomic_json(self.data / "router-workers" / (identity + ".json"), {"id": identity, "port": worker.port,
                    "version": VERSION, "token": "worker-test", "executable": "test-only"})
        self.run_server(worker)
        self.workers.append(worker)
        return worker

    def api(self, path, data=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        try:
            client.request("GET" if data is None else "POST", path, None if data is None else json.dumps(data),
                           {"X-Inspector-Control": "router-test", **(headers or {})})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def switch(self, identity):
        return self.api("/__router__/switch", {"worker_id": identity, "expected_active": self.first.worker_id})

    def test_websocket_handshake_falls_back_without_forwarding_then_http_post_succeeds(self):
        route_id = "websocket-fallback"
        self.first.save_settings({**self.first.settings, "provider_routes": {
            **self.first.settings["provider_routes"],
            route_id: {"provider": "openai", "identity": "test-openai",
                       "upstream": f"http://127.0.0.1:{self.upstream.server_port}"}}})
        prefix = "/relay/" + self.first.settings["relay_token"]
        handshake_headers = [{}, {"Connection": "Upgrade", "Upgrade": "websocket",
                                  "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"},
                             {"Sec-WebSocket-Key": "fixture"}, {"Sec-WebSocket-Version": "13"}]
        cases = [(suffix, headers) for suffix in ["/responses", "/responses/", "/responses?api-version=test",
                                                  "/p/" + route_id + "/responses?api-version=test"]
                 for headers in handshake_headers]
        for suffix, headers in cases:
            with self.subTest(suffix=suffix, headers=headers):
                client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
                self.addCleanup(client.close)
                before = len(self.seen)
                client.request("GET", prefix + suffix, headers=headers)
                response = client.getresponse()
                self.assertEqual(response.status, 426)
                self.assertEqual(json.loads(response.read())["error"]["code"], "websocket_not_supported")
                self.assertEqual(len(self.seen), before)
                client.request("POST", prefix + suffix, b'{"input":[]}',
                               {"Content-Type": "application/json", "Authorization": "Bearer fixture"})
                response = client.getresponse()
                self.assertEqual((response.status, response.read()), (200, b"ok"))
                self.assertEqual(len(self.seen), before + 1)
                self.assertEqual(self.seen[-1][1:], (b'{"input":[]}', "Bearer fixture"))

    def test_retrieving_an_existing_response_still_uses_http_get(self):
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        self.addCleanup(client.close)
        prefix = "/relay/" + self.first.settings["relay_token"]
        client.request("GET", prefix + "/responses/resp_fixture?include=usage", headers={"Authorization": "Bearer fixture"})
        response = client.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"existing-response"))
        self.assertEqual(self.seen, [("/responses/resp_fixture?include=usage", None, "Bearer fixture")])

    def test_websocket_handshake_preserves_route_and_relay_token_validation(self):
        prefix = "/relay/" + self.first.settings["relay_token"]
        for path in [prefix + "/p/missing/responses", "/relay/wrong-token/responses"]:
            with self.subTest(path=path):
                client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
                self.addCleanup(client.close)
                client.request("GET", path, headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "fixture"})
                response = client.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
        self.assertEqual(self.seen, [])

    def test_cutover_preserves_stream_and_active_records_and_sends_new_requests_to_new_worker(self):
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        path = "/relay/" + self.first.settings["relay_token"] + "/responses?api-version=test"
        client.request("POST", path, b'{"input":[]}', {"X-Test-Stream": "1", "Authorization": "Bearer fixture"})
        response = client.getresponse()
        self.assertEqual(response.read(1), b"a")
        self.assertTrue(self.started.is_set())
        packet = self.first.store.list()[0]
        self.assertEqual(packet["state"], "streaming")
        second = self.worker("2" * 32)
        self.assertEqual(second.store.get(packet["id"])[0]["state"], "streaming")
        self.assertEqual(self.switch(second.worker_id)[0], 200)
        status = self.api("/api/status")[1]
        self.assertEqual(status["worker_id"], second.worker_id)
        self.assertEqual(status["relay_active_requests"], 1)
        self.assertEqual(self.first.active_requests, 1)
        self.assertFalse(self.first.maintenance_enabled)
        fallback = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        self.addCleanup(fallback.close)
        fallback.request("GET", "/relay/" + second.settings["relay_token"] + "/responses")
        response_fallback = fallback.getresponse()
        self.assertEqual(response_fallback.status, 426)
        response_fallback.read()
        self.assertEqual(self.api("/api/stop", {})[0], 409)
        self.assertEqual(self.api("/__router__/status")[1]["active"], second.worker_id)
        self.release.set()
        self.assertEqual(response.read(), b"bcdef")
        client.close()
        self.assertEqual(self.seen[0], ("/responses?api-version=test", b'{"input":[]}', "Bearer fixture"))
        # Retire only after the old response finishes; no forced process termination.
        self.router.state["draining"][0]["not_before"] = 0
        self.router.start_monitor()
        deadline = time.monotonic() + 4
        while (self.router.state["draining"] or not second.maintenance_enabled) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.router.state["draining"], [])
        self.assertTrue(second.maintenance_enabled)
        self.assertEqual(second.store.get(packet["id"])[0]["state"], "complete")

    def test_failed_candidate_keeps_original_backend_serving(self):
        second = self.worker("2" * 32)
        original = second.worker_control
        def reject(data):
            if data.get("action") == "activate":
                raise ValueError("candidate rejected")
            return original(data)
        with patch.object(second, "worker_control", side_effect=reject):
            self.assertEqual(self.switch(second.worker_id)[0], 409)
        self.assertEqual(self.router.state["active"], self.first.worker_id)
        self.assertEqual(self.api("/api/status")[1]["worker_id"], self.first.worker_id)
        self.assertTrue(self.first.maintenance_enabled)

    def test_foreground_launch_reuses_healthy_entry(self):
        options = SimpleNamespace(data_dir=self.data, config=self.config, port=None)
        with patch("packet_inspector.router.subprocess.Popen") as launch:
            self.assertEqual(ensure_entry(options, {"port": self.router.port}), self.router.port)
        launch.assert_not_called()

    def test_second_entry_cannot_bind_a_running_entry_port(self):
        with self.assertRaises(OSError):
            RouterServer(("127.0.0.1", self.router.port), self.data, self.config)
        self.assertEqual(self.api("/api/status")[1]["worker_id"], self.first.worker_id)

    def test_launcher_recovers_when_closed_port_probe_times_out(self):
        options = SimpleNamespace(data_dir=self.data, config=self.config, port=47923)
        replies = [TimeoutError(), {"app": "codex-packet-inspector", "router_protocol": 1},
                   {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}]
        with patch("packet_inspector.router.local_json", side_effect=replies), \
                patch("packet_inspector.router.subprocess.Popen") as launch:
            self.assertEqual(ensure_entry(options, {"port": self.router.port}), 47923)
        self.assertIn("--background", launch.call_args.args[0])

    def test_cold_foreground_launch_restores_router_at_configured_port(self):
        options = SimpleNamespace(data_dir=self.data, config=self.config, port=None)
        atomic_json(self.data / "entry-port.json", {"port": 47920})
        running = []
        def api(_port, path, **_kwargs):
            if not running:
                raise ConnectionRefusedError()
            if path == "/health":
                return {"app": "codex-packet-inspector", "router_protocol": 1}
            return {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}
        def launched(*_args, **_kwargs):
            running.append(True)
            return SimpleNamespace(poll=lambda: None)
        with patch("packet_inspector.router.local_json", side_effect=api), \
                patch("packet_inspector.router.sys.frozen", True, create=True), \
                patch("packet_inspector.router.sys.executable", "installed-router.exe"), \
                patch("packet_inspector.router.subprocess.Popen", side_effect=launched) as launch:
            self.assertEqual(ensure_entry(options, {"port": self.router.port}), 47920)
        command = launch.call_args.args[0]
        self.assertEqual(command[0], "installed-router.exe")
        self.assertIn("--background", command)
        self.assertIn("--tray", command)
        self.assertEqual(command[command.index("--port") + 1], "47920")
        self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_foreground_launcher_never_accepts_a_direct_backend_as_router(self):
        options = SimpleNamespace(data_dir=self.data, config=self.config, port=None)
        with patch("packet_inspector.router.local_json", side_effect=[
                {"app": "codex-packet-inspector"},
                {"data_dir": str(self.data), "profile": {"config_path": str(self.config)}}]), \
                patch("packet_inspector.router.subprocess.Popen") as launch:
            with self.assertRaises(ValueError):
                ensure_entry(options, {"port": self.router.port})
        launch.assert_not_called()

    def test_administration_and_worker_controls_cannot_be_reached_without_capability(self):
        self.assertEqual(self.api("/__router__/status", headers={"X-Inspector-Control": "wrong"})[0], 403)
        self.assertEqual(self.api("/__worker__/control", {"action": "stop"})[0], 403)
        self.assertEqual(self.api("/api/upgrade/stop", {})[0], 403)
        self.assertEqual(self.api("/api/status", headers={"Origin": "https://example.invalid"})[0], 403)

    def test_inference_failure_is_never_replayed_on_another_worker(self):
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        client.request("POST", "/relay/" + self.first.settings["relay_token"] + "/responses", b"{}", {"X-Test-Drop": "1"})
        response = client.getresponse()
        self.assertEqual(response.status, 502)
        response.read()
        client.close()
        self.assertEqual(len(self.seen), 1)

    def test_chunked_compressed_body_and_query_are_preserved(self):
        body = gzip.compress(b'{"input":[{"role":"user","content":"fixture"}]}')
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        client.request("POST", "/relay/" + self.first.settings["relay_token"] + "/responses?version=1",
                       [body[:9], body[9:]], {"Content-Encoding": "gzip", "Authorization": "Bearer fixture"},
                       encode_chunked=True)
        response = client.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"ok"))
        client.close()
        self.assertEqual(self.seen, [("/responses?version=1", body, "Bearer fixture")])

    def test_upload_started_before_switch_remains_pinned_to_old_worker(self):
        body = b'{"input":[]}'
        client = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        client.putrequest("POST", "/relay/" + self.first.settings["relay_token"] + "/responses")
        client.putheader("Content-Length", str(len(body)))
        client.endheaders()
        client.send(body[:2])
        deadline = time.monotonic() + 2
        while not self.router.status()["relay_requests"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.router.status()["requests"].get(self.first.worker_id), 1)
        second = self.worker("2" * 32)
        self.assertEqual(self.switch(second.worker_id)[0], 200)
        self.assertEqual(self.router.status()["requests"].get(self.first.worker_id), 1)
        client.send(body[2:])
        response = client.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"ok"))
        client.close()
        self.assertEqual(self.seen, [("/responses", body, None)])


if __name__ == "__main__":
    unittest.main()
