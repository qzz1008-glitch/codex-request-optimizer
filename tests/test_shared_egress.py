"""TLS-in-TLS fixtures: no real models, logins, credentials or config changes."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from packet_inspector.egress_transport import ExitOpener, parse_egress_link
from packet_inspector.server import InspectorServer
from packet_inspector.sharing import SharingError, pinned_tls_context
from packet_inspector.sharing_role import MachineSharingRole
from packet_inspector.shared_egress import physical_tunnel
from tests.relay_fixture import HTTP_TIMEOUT


class SharedEgressTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.requests, self.dials, self.tunnel_bytes = [], [], bytearray()
        self.release_stream, self.started = threading.Event(), threading.Event()
        self.gated = False
        self.error_status = None
        fixture = self
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                fixture.requests.append((dict(self.headers), body))
                if fixture.error_status:
                    error = b'{"error":{"message":"synthetic upstream error"}}'
                    self.send_response(fixture.error_status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(error)))
                    self.send_header('Retry-After', '31')
                    self.send_header('Location', 'https://api.openai.com/must-not-follow')
                    self.end_headers()
                    self.wfile.write(error)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"private-model-answer"}\n\n')
                self.wfile.flush()
                fixture.started.set()
                if fixture.gated:
                    fixture.release_stream.wait(HTTP_TIMEOUT)
                self.wfile.write(b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":37,"output_tokens":2,"total_tokens":39}}}\n\n')
                self.wfile.flush()
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'api.openai.com')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                .not_valid_after(now+timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('api.openai.com')]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
        pem = cert.public_bytes(serialization.Encoding.PEM)
        certificate, private = self.root/'cert.pem', self.root/'key.pem'
        certificate.write_bytes(pem)
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private)
        self.trust = ssl.create_default_context(cadata=pem.decode())
        self.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        self.model.daemon_threads = True
        self.model.socket = context.wrap_socket(self.model.socket, server_side=True)
        threading.Thread(target=self.model.serve_forever, kwargs={'poll_interval':.05}, daemon=True).start()
        config = self.root/'config.toml'
        config.write_text('model="dummy"\n')
        self.relay = InspectorServer(('127.0.0.1', 0), self.root/'data', config, 'https://api.openai.com/v1')
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            self.relay.settings['egress_port'] = port.getsockname()[1]
        self.relay.sharing.role = MachineSharingRole('CodexExitTest-'+secrets.token_hex(16))
        self.relay.egress.connect_physical = self.connect
        self.openers = []

    def connect(self, host, port):
        self.dials.append((host, port))
        stream = socket.create_connection(('127.0.0.1', self.model.server_port), timeout=HTTP_TIMEOUT)
        captured = self.tunnel_bytes
        class Observed:
            def fileno(self): return stream.fileno()
            def settimeout(self, seconds): stream.settimeout(seconds)
            def recv(self, size):
                data=stream.recv(size);captured.extend(data);return data
            def sendall(self, data): captured.extend(data);stream.sendall(data)
            def close(self): stream.close()
        return Observed()

    def tearDown(self):
        self.release_stream.set()
        for opener in self.openers:
            opener.close()
        self.relay.server_close()
        self.model.shutdown()
        self.model.server_close()
        self.folder.cleanup()

    def start(self):
        return self.relay.egress.start('127.0.0.1', 0)['link']

    def opener(self, link):
        opener = ExitOpener(link, client_id=self.relay.sharing.vault.client_id, target_context=self.trust)
        self.openers.append(opener)
        return opener

    def request(self):
        return Request('https://api.openai.com/v1/responses', data=b'private-prompt-sentinel',
                       headers={'Authorization':'Bearer dummy-private-account-secret'})

    def test_tunnel_streams_and_exit_sees_neither_body_nor_native_authorization(self):
        self.gated = True
        link = self.start()
        with patch.object(self.relay.sharing, 'authorization_factory', side_effect=AssertionError('Exit requested native authorization')):
            with self.opener(link).open(self.request(), timeout=HTTP_TIMEOUT) as response:
                first = response.read1(65536)
                self.assertIn(b'private-model-answer', first)
                self.assertFalse(self.release_stream.is_set())
                self.release_stream.set()
                self.assertIn(b'response.completed', response.read())
        self.assertEqual(self.dials, [('api.openai.com',443)])
        headers, body = self.requests[0]
        self.assertEqual(body, b'private-prompt-sentinel')
        self.assertEqual(headers['Authorization'], 'Bearer dummy-private-account-secret')
        self.assertNotIn('Proxy-Authorization', headers)
        for private in (body, b'dummy-private-account-secret', b'private-model-answer'):
            self.assertNotIn(private, self.tunnel_bytes)
        self.assertEqual(self.relay.store.list(), [])
        for path in self.relay.data_dir.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-prompt-sentinel', path.read_bytes())

    def test_selected_peer_exit_is_never_used_for_incoming_tunnels(self):
        link = self.start()
        self.relay.egress.mode = 'peer'
        with patch.object(self.relay.egress, 'outgoing', side_effect=AssertionError('Re-exported another exit')):
            with self.opener(link).open(self.request(), timeout=HTTP_TIMEOUT) as response:
                self.assertEqual(response.status,200)
                response.read()
        self.assertEqual(len(self.requests),1)

    def test_only_allowed_upstream_hosts_can_be_connected_and_grants_are_revocable(self):
        link = self.start()
        host, port, grant, fingerprint = parse_egress_link(link)
        context = pinned_tls_context(host,port,fingerprint)
        with socket.create_connection((host,port),timeout=HTTP_TIMEOUT) as tcp:
            with context.wrap_socket(tcp,server_hostname=host) as stream:
                stream.sendall(f'CONNECT 127.0.0.1:47844 HTTP/1.1\r\nHost: 127.0.0.1\r\nProxy-Authorization: Bearer {grant}\r\n\r\n'.encode())
                self.assertIn(b'403',stream.recv(4096))
        self.assertFalse(self.dials)
        old = self.opener(link)
        fresh = self.relay.egress.rotate()['link']
        with self.assertRaises(OSError):
            old.open(self.request(),timeout=HTTP_TIMEOUT)
        with self.opener(fresh).open(self.request(),timeout=HTTP_TIMEOUT) as response:
            response.read()
        self.assertEqual(len(self.requests),1)

    def test_certificate_mismatch_sends_no_grant_or_model_request(self):
        link = self.start()
        bad = link.rsplit('.',1)[0]+'.'+'0'*64
        with patch.object(self.relay.egress,'authorized',wraps=self.relay.egress.authorized) as auth:
            with self.assertRaises(SharingError):
                self.opener(bad)
            self.assertEqual(auth.call_count,0)
        self.assertFalse(self.requests)

    def test_upstream_certificate_is_verified_independently_of_the_exit_pin(self):
        link = self.start()
        untrusted = ExitOpener(link, client_id=self.relay.sharing.vault.client_id)
        self.openers.append(untrusted)
        with self.assertRaises(OSError):
            untrusted.open(self.request(), timeout=HTTP_TIMEOUT)
        wrong_host = Request('https://chatgpt.com/backend-api/codex/responses', data=b'private-prompt-sentinel')
        with self.assertRaises(OSError):
            self.opener(link).open(wrong_host, timeout=HTTP_TIMEOUT)
        self.assertEqual(self.requests, [], 'Failed upstream TLS must not send a model request')

    def test_errors_and_wait_hints_are_forwarded_without_retries_or_redirects(self):
        opener = self.opener(self.start())
        for status in (429, 503, 302):
            self.error_status = status
            with opener.open(self.request(), timeout=HTTP_TIMEOUT) as response:
                self.assertEqual(response.status, status)
                self.assertEqual(response.headers.get('Retry-After'), '31')
                self.assertIn(b'synthetic upstream error', response.read())
        self.assertEqual(len(self.requests), 3)

    def test_default_local_exit_reuses_callback_and_recreates_it_after_stop(self):
        self.assertEqual(self.relay.egress.status()['selection']['mode'], 'local')
        peer = {'host':'127.0.0.1','port':47846}
        first = self.relay.egress.request_exit(peer)
        self.assertEqual(first,self.relay.egress.request_exit(peer))
        self.assertTrue(first.startswith('codex-exit://127.0.0.1:'))
        self.relay.egress.stop()
        self.assertIsNone(self.relay.egress.listener)
        self.assertEqual(self.relay.egress.status()['selection']['mode'], 'local')
        next_callback = self.relay.egress.request_exit(peer)
        self.assertTrue(next_callback.startswith('codex-exit://127.0.0.1:'))
        self.assertNotEqual(next_callback, first)
        self.assertEqual(self.relay.egress.request_exit(peer), next_callback)

    def test_distinct_credentials_allow_multiple_machines_to_share_one_physical_exit(self):
        self.gated = True
        link = self.start()
        first_opener = ExitOpener(link, client_id='a' * 64, target_context=self.trust)
        second_link = self.relay.egress.issue(label='second computer')['link']
        second_opener = ExitOpener(second_link, client_id='b' * 64, target_context=self.trust)
        self.openers.extend((first_opener, second_opener))
        with first_opener.open(self.request(), timeout=HTTP_TIMEOUT) as first:
            self.assertIn(b'private-model-answer', first.read1())
            with second_opener.open(self.request(), timeout=HTTP_TIMEOUT) as second:
                self.assertIn(b'private-model-answer', second.read1())
                self.assertEqual(self.relay.egress.status()['host']['in_flight'], 2)
                self.release_stream.set()
                self.assertIn(b'response.completed', first.read())
                self.assertIn(b'response.completed', second.read())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.relay.egress.status()['host']['connections'], 2)

    def test_exit_grants_bind_one_machine_and_allow_its_delegated_use(self):
        link = self.start()
        first = ExitOpener(link, client_id='a' * 64, target_context=self.trust)
        self.openers.append(first)
        with self.assertRaisesRegex(SharingError, '已绑定其他机器'):
            ExitOpener(link, client_id='b' * 64, target_context=self.trust)
        second = ExitOpener(link, client_id='a' * 64, target_context=self.trust)
        self.openers.append(second)
        self.assertEqual(first.snapshot['id'], second.snapshot['id'])
        for opener in (first, second):
            with opener.open(self.request(), timeout=HTTP_TIMEOUT) as response:
                self.assertIn(b'response.completed', response.read())
        self.relay.egress.opener_factory = lambda value, **kwargs: ExitOpener(value, target_context=self.trust, **kwargs)
        delegated = self.relay.egress.outgoing(link, client_id='a' * 64)
        self.openers.append(delegated)
        with delegated.open(self.request(), timeout=HTTP_TIMEOUT) as response:
            self.assertEqual(response.status, 200)
            response.read()
        with self.assertRaisesRegex(SharingError, '已绑定其他机器'):
            self.relay.egress.outgoing(link, client_id='b' * 64)
        separate = self.relay.egress.issue(label='second computer')['link']
        third = ExitOpener(separate, client_id='b' * 64, target_context=self.trust)
        self.openers.append(third)
        with third.open(self.request(), timeout=HTTP_TIMEOUT) as response:
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(len(self.requests), 4)

    def test_tunnel_does_not_add_a_one_megabyte_upload_limit(self):
        body = b'synthetic-local-upload' * 55000
        request = self.request()
        request.data = body
        with self.opener(self.start()).open(request, timeout=HTTP_TIMEOUT) as response:
            self.assertEqual(response.status, 200)
            response.read()
        self.assertGreater(len(body), 1_000_000)
        self.assertEqual(self.requests[0][1], body)

    def test_physical_exit_uses_its_system_proxy_without_another_shared_exit(self):
        seen = []
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_CONNECT(self):
                seen.append((self.path, self.headers.get('Proxy-Authorization')))
                self.send_response(200)
                self.end_headers()
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval':.05}, daemon=True).start()
        server = SimpleNamespace(opener=build_opener(ProxyHandler(
            {'https': f'http://dummy-user:dummy-pass@127.0.0.1:{proxy.server_port}'})))
        try:
            with patch('packet_inspector.shared_egress.proxy_bypass', return_value=False):
                physical_tunnel(server, 'api.openai.com', 443).close()
            self.assertEqual(seen, [('api.openai.com:443', 'Basic ZHVtbXktdXNlcjpkdW1teS1wYXNz')])
        finally:
            proxy.shutdown()
            proxy.server_close()
