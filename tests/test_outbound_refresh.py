"""New requests follow proxy changes without replaying or disrupting old ones."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

from packet_inspector.server import InspectorServer
from packet_inspector.shared_egress import physical_tunnel


class OutboundRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "config.toml"
        config.write_text('model="test"', encoding="utf8")
        self.proxies = {}
        proxies = patch("packet_inspector.server.outbound_proxies", side_effect=lambda: dict(self.proxies))
        proxies.start()
        self.addCleanup(proxies.stop)
        wsl = patch("packet_inspector.wsl_connection.LocalWsl.running", return_value=[])
        wsl.start()
        self.addCleanup(wsl.stop)
        bypass = patch("urllib.request.proxy_bypass", return_value=False)
        bypass.start()
        self.addCleanup(bypass.stop)
        self.relay = InspectorServer(("127.0.0.1", 0), root / "data", config, worker_id="1" * 32)
        self.addCleanup(self.relay.server_close)
        self.release = threading.Event()
        self.calls = []
        self.gateways = []
        for label in ("a", "b"):
            test = self
            class Gateway(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(handler):
                    body = handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
                    test.calls.append((handler.server.label, handler.path, body))
                    result = handler.server.label.encode() * 2
                    handler.send_response(200)
                    handler.send_header("Content-Length", str(len(result)))
                    handler.end_headers()
                    handler.wfile.write(result[:1])
                    handler.wfile.flush()
                    if handler.headers.get("X-Test-Hold"):
                        test.release.wait(10)
                    handler.wfile.write(result[1:])

                def do_CONNECT(handler):
                    test.calls.append((handler.server.label, "CONNECT", handler.path))
                    handler.send_response(200)
                    handler.end_headers()

            gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
            gateway.label = label
            threading.Thread(target=gateway.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
            self.gateways.append(gateway)
        self.addCleanup(self.close_gateways)

    def close_gateways(self):
        self.release.set()
        for gateway in self.gateways:
            gateway.shutdown()
            gateway.server_close()

    def select(self, index):
        address = "http://127.0.0.1:" + str(self.gateways[index].server_port)
        self.proxies = {"http": address, "https": address}
        return address

    def test_new_requests_follow_proxy_enable_region_change_and_disable(self):
        original = self.relay.outgoing_opener()
        for index, label in ((0, b"aa"), (1, b"bb")):
            self.select(index)
            opener = self.relay.egress.outgoing(own_request=True)
            with opener.open(Request("http://upstream.invalid/responses", data=b"one request"), timeout=3) as response:
                self.assertEqual(response.read(), label)
            self.assertIs(opener, self.relay.outgoing_opener())
            self.assertIsNot(opener, original)
            self.assertIs(self.relay.transport_context,
                          next(h._context for h in opener.handlers if hasattr(h, "_context")))
        self.proxies = {}
        direct = self.relay.outgoing_opener()
        self.assertEqual(direct.inspector_proxies, {})
        self.assertEqual(direct.inspector_transport_generation, 3)
        self.assertEqual([row[0] for row in self.calls], ["a", "b"])

    def test_proxy_change_preserves_an_inflight_response(self):
        self.select(0)
        old = self.relay.outgoing_opener()
        request = Request("http://upstream.invalid/responses", data=b"old", headers={"X-Test-Hold": "1"})
        with old.open(request, timeout=3) as response:
            self.assertEqual(response.read(1), b"a")
            self.select(1)
            with self.relay.outgoing_opener().open(Request("http://upstream.invalid/responses", data=b"new"), timeout=3) as newer:
                self.assertEqual(newer.read(), b"bb")
            self.release.set()
            self.assertEqual(response.read(), b"a")
        self.assertEqual([(r[0], r[2]) for r in self.calls], [("a", b"old"), ("b", b"new")])

    def test_new_shared_exit_tunnels_follow_the_current_system_proxy(self):
        with patch("packet_inspector.shared_egress.proxy_bypass", return_value=False):
            for index in (0, 1):
                self.select(index)
                physical_tunnel(self.relay, "api.openai.com", 443).close()
        self.assertEqual(self.calls, [("a", "CONNECT", "api.openai.com:443"), ("b", "CONNECT", "api.openai.com:443")])

    def test_failure_logs_the_opener_used_even_after_a_later_proxy_change(self):
        first_proxy = self.select(0)
        selected = self.relay.outgoing_opener()
        journal = self.relay.network_diagnostics
        trace = journal.request("https://upstream.invalid/responses", "POST", {}, b"{}", opener=selected)
        self.select(1)
        self.relay.outgoing_opener()
        with patch.object(journal, "_save") as save, \
                patch("packet_inspector.network_diagnostics.environment_snapshot", side_effect=lambda p: {"proxies": p}), \
                patch("packet_inspector.network_diagnostics.process_identity", return_value={}):
            trace.finish(ConnectionRefusedError(10061, "connection refused"))
        record = save.call_args.args[0]
        self.assertEqual(record["configured_proxy_for_scheme"], first_proxy)
        self.assertEqual(record["environment"]["proxies"], selected.inspector_proxies)
        self.assertEqual(record["transport_generation"], 1)
        self.assertEqual(journal.errors, 0)

    def test_successful_requests_do_not_write_diagnostic_records(self):
        self.select(0)
        opener = self.relay.outgoing_opener()
        journal = self.relay.network_diagnostics
        with patch.object(journal, "_save") as save:
            trace = journal.request("http://upstream.invalid/responses", "POST", {}, b"{}", opener=opener)
            with trace.active(), opener.open(Request("http://upstream.invalid/responses", data=b"{}"), timeout=3) as response:
                trace.response(response)
                trace.feed(response.read())
            trace.finish()
        save.assert_not_called()
