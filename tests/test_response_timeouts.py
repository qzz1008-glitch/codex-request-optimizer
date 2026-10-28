"""Active streams outlive one read interval; silent streams are closed."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from packet_inspector.http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from packet_inspector.server import Handler, InspectorServer


FIRST = b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
DELTA = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
DONE = b'data: ' + json.dumps({"type": "response.completed", "response": {
    "status": "completed", "output": [{"type": "message", "role": "assistant",
    "content": [{"type": "output_text", "text": "summary"}]}]}}).encode() + b'\n\n'


class ResponseTimeoutTests(unittest.TestCase):
    @contextmanager
    def relay(self, *, silent=False):
        stop = threading.Event()

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(FIRST)
                    self.wfile.flush()
                    if silent:
                        stop.wait(3)
                        return
                    for _ in range(8):
                        if stop.wait(.1):
                            return
                        self.wfile.write(DELTA)
                        self.wfile.flush()
                    self.wfile.write(DONE)
                    self.wfile.flush()
                except OSError:
                    pass  # An idle timeout or test cleanup can close the client.
                finally:
                    self.close_connection = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model="test"\n', encoding="utf8")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            server = InspectorServer(("127.0.0.1", 0), root / "data", config,
                                     f"http://127.0.0.1:{upstream.server_port}")
            server.settings.update(auto_detect_providers=False, packet_size_protection_enabled=False,
                                   capacity_retry_enabled=False)
            for item in (upstream, server):
                threading.Thread(target=item.serve_forever, daemon=True).start()
            try:
                with patch("packet_inspector.server.UPSTREAM_READ_TIMEOUT_SECONDS", .5):
                    yield server
            finally:
                stop.set()
                for item in (server, upstream):
                    item.shutdown()
                    item.server_close()

    def request(self, server, *, on_first=None):
        client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            client.request("POST", "/relay/" + server.settings["relay_token"] + "/responses",
                           b'{"model":"test","stream":true,"input":[]}',
                           {"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            if on_first is not None:
                first = response.read(len(FIRST))
                on_first()
                return first + response.read()
            return response.read()
        finally:
            client.close()

    def test_active_generation_can_outlive_the_idle_interval(self):
        with self.relay() as server:
            started = time.monotonic()
            self.assertEqual(self.request(server), FIRST + DELTA * 8 + DONE)
            self.assertGreater(time.monotonic() - started, .5)

    def test_silent_generation_closes_without_a_fabricated_completion(self):
        with self.relay(silent=True) as server:
            first_received = []
            self.assertEqual(self.request(server, on_first=lambda: first_received.append(time.monotonic())), FIRST)
            # Measure the silent interval, excluding request preparation and DB writes.
            self.assertLess(time.monotonic() - first_received[0], 2)

    def test_overflow_summary_uses_the_same_idle_interval(self):
        with self.relay() as server:
            handler = Handler.__new__(Handler)
            handler.server = server
            self.assertEqual(handler._checkpoint(server.settings["upstream"], "/responses",
                                               {"Content-Type": "application/json"}, b"{}", "identity"),
                             "summary")

    def test_silent_overflow_summary_times_out(self):
        with self.relay(silent=True) as server:
            handler = Handler.__new__(Handler)
            handler.server = server
            with self.assertRaises(TimeoutError):
                handler._checkpoint(server.settings["upstream"], "/responses", {}, b"{}", "identity")

    def test_default_matches_codex_five_minute_idle_timeout(self):
        self.assertEqual(UPSTREAM_READ_TIMEOUT_SECONDS, 300)
