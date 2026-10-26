"""Below the predictive threshold, responses remain byte-for-byte intact."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from packet_inspector.server import InspectorServer


class ResponseIntegrityTests(unittest.TestCase):
    def test_below_predictive_threshold_json_and_stream_keep_authentic_usage(self):
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                body = self.server.response_body
                self.send_response(200)
                self.send_header("Content-Type", self.server.response_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"authentic-usage"')
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model="test"\n', encoding="utf8")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            relay = InspectorServer(("127.0.0.1", 0), root / "data", config,
                                    f"http://127.0.0.1:{upstream.server_port}")
            relay.settings.update(packet_size_protection_enabled=True,
                                  budget_bytes=120_000, auto_detect_providers=False)
            for server in (upstream, relay):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                body = json.dumps({"model": "test", "input": [{"role": "user",
                                  "content": "x" * 55_000}]}).encode()
                usage = {"status": "completed", "usage": {
                    "input_tokens": 40, "output_tokens": 2, "total_tokens": 42}}
                responses = [
                    ("application/json", json.dumps(usage).encode()),
                    ("text/event-stream", b"data: " + json.dumps({
                        "type": "response.completed", "response": usage}).encode() + b"\n\n"),
                ]
                for content_type, expected in responses:
                    with self.subTest(content_type=content_type):
                        upstream.response_type, upstream.response_body = content_type, expected
                        client = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=5)
                        try:
                            path = "/relay/" + relay.settings["relay_token"] + "/responses"
                            client.request("POST", path, body, {"Content-Type": "application/json"})
                            response = client.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), expected)
                            self.assertEqual(response.getheader("ETag"), '"authentic-usage"')
                        finally:
                            client.close()
                self.assertEqual(received, [body, body])
                self.assertEqual(config.read_text(encoding="utf8"), 'model="test"\n')
            finally:
                for server in (relay, upstream):
                    server.shutdown()
                    server.server_close()
