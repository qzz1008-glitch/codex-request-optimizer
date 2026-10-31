"""Live TLS streams cross worker switches using isolated fake accounts and exits."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from packet_inspector import VERSION
from packet_inspector.egress_transport import EGRESS_HEADER, ExitOpener
from packet_inspector.router import RouterServer, atomic_json, control_worker
from packet_inspector.server import InspectorServer
from packet_inspector.sharing import parse_link, pinned_tls_context
from packet_inspector.sharing_credentials import CLIENT_HEADER, CredentialVault
from packet_inspector.sharing_handover import prepare_upgrade, finish_upgrade
from packet_inspector.sharing_role import MachineSharingRole
from tests.test_sharing import DummyAuthorization, free_port


@unittest.skipUnless(os.name == "nt", "Windows listener handover")
class SharingHandoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.data = self.root / "config.toml", self.root / "data"
        self.config.write_text('model="synthetic"\n', encoding="utf8")
        self.gate, self.started = threading.Event(), threading.Event()
        self.clients, self.servers, self.bodies = [], [], []
        fixture = self
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fixture.bodies.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"start"}\n\n')
                self.wfile.flush()
                if body == b'"held"':
                    fixture.started.set()
                    fixture.gate.wait(10)
                self.wfile.write(b'data: {"type":"response.completed","response":{"status":"completed",'
                                 b'"usage":{"input_tokens":37,"output_tokens":2,"total_tokens":39}}}\n\n')
                self.wfile.flush()
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        self.model.daemon_threads = True
        threading.Thread(target=self.model.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.target = f"http://127.0.0.1:{self.model.server_port}"
        self.role_name = "CodexSharingHandoverTest-" + secrets.token_hex(16)
        self.old, self.new = self.make_worker("a" * 32), self.make_worker("b" * 32)
        atomic_json(self.data / "router-state.json", {"protocol": 1, "active": self.old.worker_id,
                    "draining": [], "config": str(self.config.resolve()), "token": "fixture-router"})
        self.router = RouterServer(("127.0.0.1", 0), self.data, self.config)
        threading.Thread(target=self.router.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.old.worker_control({"action": "activate", "public_port": self.router.port, "maintenance": False})
        self.new.public_port = self.router.port
        self.client_id = CredentialVault(self.root / "guest").client_id
        self.addCleanup(self.close_servers)

    def make_worker(self, identity):
        with patch("packet_inspector.wsl_connection.LocalWsl.running", return_value=[]):
            server = InspectorServer(("127.0.0.1", 0), self.data, self.config, self.target,
                                     worker_id=identity, worker_token="fixture-" + identity)
        server.sharing.role = MachineSharingRole(self.role_name)
        server.sharing.authorization_factory = DummyAuthorization
        server.sharing._host_upstream = lambda _credentials: self.target
        server.opener = build_opener(ProxyHandler({}))
        server.egress.opener_factory = lambda *_args, **_kwargs: build_opener(ProxyHandler({}))
        server.connection_resume.schedule = lambda: None
        self.servers.append(server)
        atomic_json(self.data / "router-workers" / (identity + ".json"),
                    {"id": identity, "port": server.port, "token": server.worker_token,
                     "version": VERSION, "executable": str(self.data / "bin/versions/fixture/app.exe")})
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        return server

    def close_servers(self):
        self.gate.set()
        for client in self.clients:
            client.close()
        deadline = time.monotonic() + 5
        while any(s.sharing_handover.connections() or s.active_requests for s in self.servers) and time.monotonic() < deadline:
            time.sleep(.01)
        self.router.shutdown()
        self.router.server_close()
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()
        self.model.shutdown()
        self.model.server_close()

    def share_quota(self):
        result = self.old.sharing.start_host("127.0.0.1", free_port())
        self.host, self.port, self.token, self.fingerprint = parse_link(result["link"])
        self.context = pinned_tls_context(self.host, self.port, self.fingerprint)
        return result["link"]

    def quota_request(self, body):
        client = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=5)
        self.clients.append(client)
        client.request("POST", "/v1/responses", body, headers={"Authorization": "Bearer " + self.token,
                       CLIENT_HEADER: self.client_id, EGRESS_HEADER: "synthetic-exit", "Content-Type": "application/json"})
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        return response

    def switch(self):
        ticket = prepare_upgrade(self.data, self.old.worker_id, self.new.worker_id)
        self.router.switch(self.new.worker_id, self.old.worker_id)
        return ticket

    def quota_peer(self):
        peer_root = self.root / "peer"
        peer_root.mkdir()
        config = peer_root / "config.toml"
        config.write_text('model="peer"\n', encoding="utf8")
        peer = InspectorServer(("127.0.0.1", 0), peer_root / "data", config, self.target)
        peer.sharing.role = MachineSharingRole(self.role_name + "-peer")
        peer.sharing.authorization_factory = DummyAuthorization
        peer.sharing._host_upstream = lambda _credentials: self.target
        self.servers.append(peer)
        threading.Thread(target=peer.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        return peer

    def test_quota_stream_and_new_requests_keep_original_port_certificate_and_grant(self):
        link = self.share_quota()
        before = self.config.read_bytes()
        response = self.quota_request(b'"held"')
        self.assertTrue(self.started.wait(2))
        ticket = self.switch()
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.new.sharing.host["port"], self.port)
        self.assertEqual(self.new.sharing.host["fingerprint"], self.fingerprint)
        self.assertIn(b'"response.completed"', self.quota_request(b'"new"').read())
        with self.assertRaisesRegex(ValueError, "409"):
            control_worker(self.data, self.old.worker_id, "stop")
        for path in ("/api/stop", "/api/upgrade/stop"):
            client = http.client.HTTPConnection("127.0.0.1", self.old.port, timeout=3)
            self.clients.append(client)
            client.request("POST", path, b"{}")
            stop_response = client.getresponse()
            self.assertEqual(stop_response.status, 409)
            stop_response.read()
        self.gate.set()
        self.assertIn(b'"response.completed"', response.read())
        self.assertEqual(self.bodies.count(b'"held"'), 1)
        self.assertEqual(self.bodies.count(b'"new"'), 1)
        row = next(row for row in self.new.sharing.vault.rows() if row["id"] == self.new.sharing.host["last_credential_id"])
        self.assertEqual(row["totals"]["input_tokens"], 74)
        self.assertEqual(row["totals"]["requests"], 2)
        # Closing the old controller must not invalidate the newly adopted role.
        self.old.sharing.close()
        contender = MachineSharingRole(self.role_name)
        with self.assertRaisesRegex(Exception, "本机已有实例"):
            contender.acquire()
        self.assertEqual(self.new.sharing.credential_link(self.new.sharing.host["last_credential_id"]), link)

    def test_switch_rollback_keeps_the_old_quota_service_and_live_stream(self):
        self.share_quota()
        response = self.quota_request(b'"held"')
        ticket = self.switch()
        candidate_response = self.quota_request(b'"held"')
        self.router.switch(self.old.worker_id, self.new.worker_id)
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.assertIn(b'"response.completed"', self.quota_request(b'"after rollback"').read())
        with self.assertRaisesRegex(ValueError, "409"):
            control_worker(self.data, self.new.worker_id, "stop")
        self.gate.set()
        self.assertIn(b'"response.completed"', response.read())
        self.assertIn(b'"response.completed"', candidate_response.read())
        self.assertEqual(self.bodies.count(b'"held"'), 2)
        self.assertTrue(self.old.sharing.owns_provider_state)
        self.assertFalse(self.new.sharing.owns_provider_state)

    def test_local_and_shared_streams_continue_together_without_replaying_requests(self):
        self.share_quota()
        shared = self.quota_request(b'"held"')
        local = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=5)
        self.clients.append(local)
        path = "/relay/" + self.old.settings["relay_token"] + "/responses"
        local.request("POST", path, b'"held"', headers={"Authorization": "Bearer synthetic-local"})
        response = local.getresponse()
        self.assertEqual(response.status, 200)
        ticket = self.switch()
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.gate.set()
        self.assertIn(b'"response.completed"', shared.read())
        self.assertIn(b'"response.completed"', response.read())
        self.assertEqual(self.bodies.count(b'"held"'), 2)

    def test_borrowed_quota_and_exit_keep_provider_and_selected_credentials(self):
        peer = self.quota_peer()
        quota_link = peer.sharing.start_host("127.0.0.1", free_port())["link"]
        exit_link = peer.egress.start("127.0.0.1", free_port())["link"]
        self.old.sharing.connect(quota_link, label="quota fixture")
        self.old.egress.opener_factory = self.new.egress.opener_factory = ExitOpener
        self.old.egress.select("peer", exit_link, label="network fixture")
        selected = self.old.sharing.remote["credential_id"], self.old.egress.peer_id
        before = self.config.read_bytes()
        ticket = self.switch()
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.new.sharing.client_route()["credential_id"], selected[0])
        self.assertEqual(self.new.egress.peer_id, selected[1])
        self.assertEqual(self.new.egress.request_exit(), exit_link)
        self.old.sharing.close()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(self.new.sharing.provider.path.exists())

    def test_first_borrowed_quota_request_uses_the_same_callback_after_upgrade(self):
        peer = self.quota_peer()
        link = peer.sharing.start_host("127.0.0.1", free_port())["link"]
        self.old.sharing.connect(link, label="quota fixture")
        self.old.settings["egress_port"] = free_port()
        self.assertIsNone(self.old.egress.listener)
        before = self.config.read_bytes()
        ticket = prepare_upgrade(self.data, self.old.worker_id, self.new.worker_id)
        callback = self.old.egress.callback_link
        self.assertTrue(callback)
        self.assertFalse(self.new.sharing.owns_provider_state)
        self.router.switch(self.new.worker_id, self.old.worker_id)
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.assertEqual(self.new.egress.request_exit(self.new.sharing.client_route()), callback)
        self.old.sharing.close()
        self.old.egress.close()
        self.assertEqual(self.config.read_bytes(), before)
        opener = ExitOpener(callback, client_id=self.new.sharing.vault.client_id)
        self.addCleanup(opener.close)
        self.assertEqual(opener.snapshot["kind"], "exit")

    def test_closing_a_staged_borrower_does_not_restore_active_configuration(self):
        peer = self.quota_peer()
        link = peer.sharing.start_host("127.0.0.1", free_port())["link"]
        self.old.sharing.connect(link, label="quota fixture")
        self.old.settings["egress_port"] = free_port()
        before = self.config.read_bytes()
        ticket = prepare_upgrade(self.data, self.old.worker_id, self.new.worker_id)
        self.new.sharing.close()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(self.old.sharing.provider.path.exists())
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        self.assertTrue(self.old.sharing.owns_provider_state)

    def test_configuration_changes_are_rejected_only_during_the_handover(self):
        self.share_quota()
        state = control_worker(self.data, self.old.worker_id, "sharing_export", target=self.new.worker_id)
        before = self.config.read_bytes()
        client = http.client.HTTPConnection("127.0.0.1", self.old.port, timeout=3)
        self.clients.append(client)
        client.request("POST", "/api/sharing", json.dumps({"action": "host_stop"}),
                       headers={"Content-Type": "application/json"})
        response = client.getresponse()
        self.assertEqual(response.status, 409)
        response.read()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn(b'"response.completed"', self.quota_request(b'"continues"').read())
        control_worker(self.data, self.old.worker_id, "sharing_finish", ticket=state["ticket"], committed=False)
        self.assertFalse(self.old.sharing_handover.pending)

    def test_abandoned_prepare_releases_candidate_handles_without_stopping_old_service(self):
        self.share_quota()
        prepare_upgrade(self.data, self.old.worker_id, self.new.worker_id)
        for worker in (self.new, self.old):
            worker.sharing_handover.deadline = 0
            self.assertFalse(worker.sharing_handover.poll())
        self.assertEqual(self.new.sharing.listener.socket.fileno(), -1)
        self.assertIsNone(self.new.sharing.role.resource)
        self.assertTrue(self.old.sharing.owns_provider_state)
        self.assertIn(b'"response.completed"', self.quota_request(b'"old continues"').read())

    def test_abandoned_finalization_uses_committed_router_owner_and_preserves_live_stream(self):
        self.share_quota()
        response = self.quota_request(b'"held"')
        self.switch()
        for worker in (self.old, self.new):
            worker.sharing_handover.deadline = 0
            self.assertFalse(worker.sharing_handover.poll())
        self.assertTrue(self.new.sharing.owns_provider_state)
        self.assertFalse(self.old.sharing.owns_provider_state)
        self.assertEqual(self.old.sharing.listener.socket.fileno(), -1)
        self.assertIn(b'"response.completed"', self.quota_request(b'"new continues"').read())
        self.gate.set()
        self.assertIn(b'"response.completed"', response.read())

    def test_native_authorization_failure_preserves_old_service_and_records_bounded_error(self):
        self.share_quota()
        changed = DummyAuthorization()
        changed.identity = "different-account"
        self.new.sharing.authorization_factory = lambda: changed
        with self.assertRaisesRegex(ValueError, "400"):
            prepare_upgrade(self.data, self.old.worker_id, self.new.worker_id)
        self.assertIn(b'"response.completed"', self.quota_request(b'"still old"').read())
        report = json.loads((self.data / "sharing-handover.json").read_text(encoding="utf8"))
        self.assertEqual(report["last_error"]["step"], "account_identity")
        for _ in range(20):
            self.old.sharing_handover._record("fixture")
        report = json.loads((self.data / "sharing-handover.json").read_text(encoding="utf8"))
        self.assertLessEqual(len(report["events"]), 12)
        self.assertLess((self.data / "sharing-handover.json").stat().st_size, 10000)
        self.assertNotIn(self.token, json.dumps(report))

    def test_pending_tls_handshake_is_counted_and_can_finish_after_handover(self):
        self.share_quota()
        raw = socket.create_connection((self.host, self.port), timeout=3)
        self.clients.append(raw)
        deadline = time.monotonic() + 2
        while not self.old.sharing_handover.connections() and time.monotonic() < deadline:
            time.sleep(.01)
        ticket = self.switch()
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        with self.assertRaisesRegex(ValueError, "409"):
            control_worker(self.data, self.old.worker_id, "stop")
        stream = self.context.wrap_socket(raw, server_hostname=self.host)
        self.clients.append(stream)
        stream.sendall(("GET /v1/status HTTP/1.1\r\nHost: " + self.host + "\r\nAuthorization: Bearer " + self.token +
                        "\r\n" + CLIENT_HEADER + ": " + self.client_id + "\r\nConnection: close\r\n\r\n").encode())
        self.assertIn(b"200 OK", stream.recv(4096))

    def test_network_tunnel_and_new_connections_survive_switch(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                while data := self.request.recv(4096):
                    self.request.sendall(data)
        echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        echo.daemon_threads = True
        threading.Thread(target=echo.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.addCleanup(echo.server_close)
        self.addCleanup(echo.shutdown)
        for server in self.servers:
            server.egress.connect_physical = lambda *_args: socket.create_connection(echo.server_address, timeout=3)
        result = self.old.egress.start("127.0.0.1", free_port())
        from packet_inspector.egress_transport import parse_egress_link
        host, port, token, fingerprint = parse_egress_link(result["link"])
        context = pinned_tls_context(host, port, fingerprint)
        def tunnel():
            stream = context.wrap_socket(socket.create_connection((host, port), timeout=3), server_hostname=host)
            self.clients.append(stream)
            stream.sendall(("CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\nProxy-Authorization: Bearer " +
                            token + "\r\n" + CLIENT_HEADER + ": " + self.client_id + "\r\n\r\n").encode())
            header = b""
            while b"\r\n\r\n" not in header:
                part = stream.recv(4096)
                self.assertTrue(part)
                header += part
                self.assertLess(len(header), 8192)
            self.assertIn(b"200 Connection established", header)
            return stream
        original = tunnel()
        original.sendall(b"before")
        self.assertEqual(original.recv(6), b"before")
        ticket = self.switch()
        finish_upgrade(self.data, self.old.worker_id, self.new.worker_id, ticket)
        original.sendall(b"after")
        self.assertEqual(original.recv(5), b"after")
        fresh = tunnel()
        fresh.sendall(b"new")
        self.assertEqual(fresh.recv(3), b"new")
        self.assertEqual(self.new.egress.local_link, result["link"])
        with self.assertRaisesRegex(ValueError, "409"):
            control_worker(self.data, self.old.worker_id, "stop")

    def test_standby_start_and_close_do_not_restore_active_shared_provider(self):
        self.old.sharing.provider.activate(self.old.sharing.client_endpoint, 15, label="synthetic")
        before = self.config.read_bytes()
        extra = self.make_worker("c" * 32)
        self.assertEqual(self.config.read_bytes(), before)
        extra.sharing.close()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(self.old.sharing.provider.path.exists())

    def test_listening_socket_moves_to_an_actual_new_process(self):
        self.share_quota()
        held = self.quota_request(b'"held"')
        identity = "c" * 32
        script = r'''
import json,sys
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from packet_inspector.server import InspectorServer
from packet_inspector.sharing_role import MachineSharingRole
from packet_inspector.wsl_connection import LocalWsl
from tests.test_sharing import DummyAuthorization
LocalWsl.running=lambda _self: []
data,config,target,identity,role=sys.argv[1:]
server=InspectorServer(('127.0.0.1',0),Path(data),Path(config),target,
                       worker_id=identity,worker_token='fixture-child')
server.sharing.role=MachineSharingRole(role)
server.sharing.authorization_factory=DummyAuthorization
server.opener=build_opener(ProxyHandler({}))
server.egress.opener_factory=lambda *_args,**_kwargs: build_opener(ProxyHandler({}))
server.connection_resume.schedule=lambda: None
print(json.dumps({'port':server.port}),flush=True)
try:
    server.serve_forever(poll_interval=.05)
finally:
    server.server_close()
'''
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(self.data), str(self.config),
                                    self.target, identity, self.role_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            ready, lines = threading.Event(), []
            def read_ready():
                lines.append(process.stdout.readline())
                ready.set()
            threading.Thread(target=read_ready, daemon=True).start()
            self.assertTrue(ready.wait(10), "isolated worker did not start")
            self.assertTrue(lines[0], "isolated worker exited before its health endpoint")
            port = json.loads(lines[0])["port"]
            atomic_json(self.data / "router-workers" / (identity + ".json"),
                        {"id": identity, "port": port, "token": "fixture-child", "version": VERSION,
                         "executable": str(self.data / "bin/versions/fixture/child.exe")})
            ticket = prepare_upgrade(self.data, self.old.worker_id, identity)
            self.router.switch(identity, self.old.worker_id)
            finish_upgrade(self.data, self.old.worker_id, identity, ticket)
            self.assertIn(b'"response.completed"', self.quota_request(b'"new process"').read())
            self.gate.set()
            self.assertIn(b'"response.completed"', held.read())
            self.assertEqual(self.bodies.count(b'"new process"'), 1)
            control_worker(self.data, identity, "stop")
            _output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error[-1000:])
        finally:
            if process.poll() is None:
                process.terminate()  # Only the isolated synthetic worker owned by this test.
                process.communicate(timeout=5)

