"""Entry configuration must not stop or reconfigure the live relay."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.entry_port import CONFIG_NAME, configured_port, port_status, save_port
from packet_inspector.server import InspectorServer, argument_parser


class EntryPortTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n', encoding="utf8")
        self.server = InspectorServer(("127.0.0.1", 0), self.data, self.config, "http://127.0.0.1:9/v1")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.ui = DesktopUI(self.server.port, read_only=True).start()
        self.addCleanup(self.ui.close)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def api(self, path, value=None, *, port=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port or self.server.port, timeout=3)
        try:
            body = None if value is None else json.dumps(value).encode()
            connection.request("GET" if value is None else "POST", path, body, headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_preview_update_settings_are_independent_of_live_relay_settings(self):
        before_settings = self.server.settings_path.read_bytes()
        before_config = self.config.read_bytes()
        code, updates = self.api("/api/updates", port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertTrue(updates["supported"])
        self.assertFalse(updates["automatic"])
        code, updates = self.api("/api/updates", {"action": "configure", "automatic": False}, port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertFalse(updates["automatic"])
        self.assertEqual(self.server.settings_path.read_bytes(), before_settings)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.api("/api/updates", {"action": "install"}, port=self.ui.port,
                                  headers={"Origin": "https://example.invalid"})[0], 403)

    def test_preview_can_save_only_entry_port_without_changing_live_settings_or_provider_config(self):
        before_settings = self.server.settings_path.read_bytes()
        before_config = self.config.read_bytes()
        desired = 47844 if self.server.port != 47844 else 47846
        code, status = self.api("/api/status", port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertTrue(status["ui_read_only"])
        self.assertTrue(status["ui_entry_port_editable"])
        code, result = self.api("/api/entry-port", {"port": desired}, port=self.ui.port)
        self.assertEqual(code, 200)
        self.assertEqual(result, {"current_port": self.server.port, "configured_port": desired, "pending": True})
        self.assertEqual(configured_port(self.data), desired)
        self.assertEqual(self.api("/api/settings", {"budget_bytes": 2000}, port=self.ui.port)[0], 409)
        self.assertEqual(self.server.settings_path.read_bytes(), before_settings)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.api("/health")[0], 200)

    def test_server_api_and_startup_read_same_configuration_and_cli_override_remains_available(self):
        self.assertEqual(self.api("/api/entry-port", {"port": 48765})[0], 200)
        options = argument_parser().parse_args(["--data-dir", str(self.data)])
        self.assertEqual(options.port, 48765)
        self.assertEqual(argument_parser().parse_args(["--data-dir", str(self.data), "--port", "0"]).port, 0)
        self.assertEqual(self.api("/api/status")[1]["entry_port"]["configured_port"], 48765)
        self.assertEqual(self.api("/api/entry-port")[1]["current_port"], self.server.port)

    def test_invalid_values_and_cross_origin_write_are_rejected_without_creating_config(self):
        for listen_port in [0, 1023, 65536, True, "47844", None, 47844.5]:
            for target in [self.server.port, self.ui.port]:
                with self.subTest(value=listen_port, target=target):
                    self.assertEqual(self.api("/api/entry-port", {"port": listen_port}, port=target)[0], 400)
        for target in [self.server.port, self.ui.port]:
            self.assertEqual(self.api("/api/entry-port", {"port": 47846}, port=target,
                                      headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertFalse((self.data / CONFIG_NAME).exists())

    def test_corrupt_port_file_does_not_hide_live_status_and_can_be_repaired(self):
        (self.data / CONFIG_NAME).write_text("{broken", encoding="utf8")
        for target in [self.server.port, self.ui.port]:
            code, status = self.api("/api/status", port=target)
            self.assertEqual(code, 200)
            self.assertIn("error", status["entry_port"])
        self.assertEqual(self.api("/api/entry-port", {"port": 47846})[0], 200)
        self.assertEqual(port_status(self.data, self.server.port)["configured_port"], 47846)

    def test_saving_current_port_has_no_pending_change_and_cleans_temporary_file(self):
        result = save_port(self.data, self.server.port, self.server.port)
        self.assertFalse(result["pending"])
        self.assertEqual(list(self.data.glob(CONFIG_NAME + ".*.tmp")), [])

    def test_saving_port_does_not_interrupt_streaming_response(self):
        started, release = threading.Event(), threading.Event()

        class Streaming(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(handler):
                handler.rfile.read(int(handler.headers["Content-Length"]))
                handler.send_response(200)
                handler.send_header("Content-Length", "6")
                handler.end_headers()
                handler.wfile.write(b"a")
                handler.wfile.flush()
                started.set()
                release.wait(3)
                handler.wfile.write(b"bcdef")

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Streaming)
        upstream_thread = threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        upstream_thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=4)
        try:
            self.server.save_settings({**self.server.settings, "upstream": f"http://127.0.0.1:{upstream.server_port}",
                                       "request_compression": "off"})
            connection.request("POST", "/relay/" + self.server.settings["relay_token"] + "/responses", b"{}")
            response = connection.getresponse()
            self.assertEqual(response.read(1), b"a")
            self.assertTrue(started.is_set())
            self.assertEqual(self.server.active_requests, 1)
            self.assertEqual(self.api("/api/entry-port", {"port": 47846}, port=self.ui.port)[0], 200)
            self.assertEqual(self.server.active_requests, 1)
            release.set()
            self.assertEqual(response.read(), b"bcdef")
            self.assertEqual(self.api("/health")[0], 200)
        finally:
            release.set()
            connection.close()
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
