"""A separate exit capability. Incoming tunnels always use the physical exit."""
import base64
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
import select
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, proxy_bypass

from .egress_transport import EGRESS_PROTOCOL, ExitOpener, UPSTREAM_HOSTS, parse_egress_link
from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .sharing import LANServer, SharingError, lan_address, tls_context


def physical_tunnel(server, host, port):
    """Use OS/environment proxy settings, never another shared exit selection."""
    proxies = next((handler.proxies for handler in server.opener.handlers if isinstance(handler, ProxyHandler)), {})
    value = None if proxy_bypass(host) else proxies.get("https")
    if not value:
        return socket.create_connection((host, port), timeout=8)
    proxy = urlsplit(value if "://" in value else "http://" + value)
    if proxy.scheme not in {"http", "https"} or not proxy.hostname:
        raise OSError("unsupported physical proxy")
    connection = HTTPConnection(proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80), timeout=8)
    try:
        headers = {}
        if proxy.username is not None:
            credential = unquote(proxy.username) + ":" + unquote(proxy.password or "")
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credential.encode()).decode()
        connection.set_tunnel(host, port, headers=headers)
        connection.sock = socket.create_connection((connection.host, connection.port), timeout=8)
        if proxy.scheme == "https":
            connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=proxy.hostname)
        connection._tunnel()
        tunnel, connection.sock = connection.sock, None
        return tunnel
    finally:
        connection.close()


class ExitHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path != "/v1/status":
            self.reply(404, {"error": "exit_route_not_found"})
        elif not self.server.controller.authorized(self.headers.get("Authorization", "")):
            self.reply(403, {"error": "exit_unavailable"})
        else:
            self.reply(200, {"kind": "network_exit", "protocol": EGRESS_PROTOCOL})

    def do_CONNECT(self):
        controller = self.server.controller
        if self.path not in {host + ":443" for host in UPSTREAM_HOSTS}:
            self.reply(403, {"error": "exit_target_forbidden"})
            return
        reservation = controller.reserve(self.headers.get("Proxy-Authorization", ""))
        if reservation is None:
            self.reply(403, {"error": "exit_unavailable"})
            return
        upstream = None
        sent = received = 0
        established = False
        try:
            upstream = controller.connect_physical(self.path[:-4], 443)
            upstream.settimeout(UPSTREAM_READ_TIMEOUT_SECONDS)
            self.connection.settimeout(UPSTREAM_READ_TIMEOUT_SECONDS)
            self.send_response(200, "Connection established")
            self.end_headers()
            established = True
            last = time.monotonic()
            while time.monotonic() - last < UPSTREAM_READ_TIMEOUT_SECONDS:
                pending = [stream for stream in (self.connection, upstream)
                           if isinstance(stream, ssl.SSLSocket) and stream.pending()]
                readable = pending or select.select([self.connection, upstream], [], [], 1)[0]
                if not readable:
                    continue
                for stream in readable:
                    data = stream.recv(65536)
                    if not data:
                        return
                    if stream is self.connection:
                        upstream.sendall(data)
                        sent += len(data)
                    else:
                        self.connection.sendall(data)
                        received += len(data)
                    last = time.monotonic()
        except (OSError, ValueError):
            if not established:
                self.reply(502, {"error": "physical_exit_unreachable"})
        finally:
            if upstream:
                upstream.close()
            controller.release(reservation, sent, received)
            self.close_connection = True

    def do_POST(self):
        self.reply(405, {"error": "connect_tunnel_required"})


class SharedEgress:
    def __init__(self, server):
        self.server = server
        self.guard = threading.RLock()
        self.listener = self.timer = None
        self.host = {}
        self.local_link = ""
        self.mode = "provider"
        self.peer_link = ""
        self.peer_opener = None
        self.connect_physical = lambda host, port: physical_tunnel(server, host, port)
        self.opener_factory = ExitOpener

    def _stop_timer(self):
        timer, self.timer = self.timer, None
        if timer:
            timer.cancel()

    def start(self, address, port=47847, hours=8):
        address = lan_address(address)
        if type(port) is not int or not 0 <= port <= 65535 or type(hours) is not int or not 1 <= hours <= 24:
            raise SharingError("出口端口或时长不在允许范围。")
        with self.guard:
            if self.listener:
                return {**self.status(), "link": self.local_link}
            context, fingerprint = tls_context(address)
            try:
                listener = LANServer((address, port), self, context, ExitHandler)
            except OSError:
                raise SharingError("无法开启共享出口，请检查本机地址、端口和防火墙。") from None
            grant = secrets.token_urlsafe(32)
            self.host = {"address": address, "port": listener.server_port, "expires_at": time.time() + hours * 3600,
                         "grant_hash": hashlib.sha256(grant.encode()).digest(), "fingerprint": fingerprint,
                         "connections": 0, "in_flight": 0, "sent_bytes": 0, "received_bytes": 0}
            self.listener = listener
            self.local_link = f"codex-exit://{address}:{listener.server_port}/#{grant}.{fingerprint}"
            threading.Thread(target=listener.serve_forever, name="codex-shared-exit", daemon=True).start()
            state = self.host
            self.timer = threading.Timer(hours * 3600, lambda: self.stop(expected=state))
            self.timer.daemon = True
            self.timer.start()
            return {**self.status(), "link": self.local_link}

    def authorized(self, value):
        if not value.startswith("Bearer "):
            return False
        with self.guard:
            return bool(self.listener and time.time() < self.host["expires_at"]
                        and secrets.compare_digest(hashlib.sha256(value[7:].encode()).digest(), self.host["grant_hash"]))

    def reserve(self, value):
        with self.guard:
            if not self.authorized(value):
                return None
            self.host["connections"] += 1
            self.host["in_flight"] += 1
            return self.host

    def release(self, reservation, sent, received):
        with self.guard:
            reservation["in_flight"] -= 1
            reservation["sent_bytes"] += sent
            reservation["received_bytes"] += received

    def stop(self, *, expected=None):
        with self.guard:
            if expected is not None and self.host is not expected:
                return self.status()
            listener, self.listener = self.listener, None
            self._stop_timer()
            self.local_link = ""
            self.host.pop("grant_hash", None)
            if expected is None and self.mode == "local":
                self.mode = "provider"
        if listener:
            listener.shutdown()
            listener.server_close()
        return self.status()

    def rotate(self):
        with self.guard:
            if not self.listener or time.time() >= self.host["expires_at"]:
                raise SharingError("请先开启共享出口。")
            grant = secrets.token_urlsafe(32)
            self.host["grant_hash"] = hashlib.sha256(grant.encode()).digest()
            self.local_link = f"codex-exit://{self.host['address']}:{self.host['port']}/#{grant}.{self.host['fingerprint']}"
            return {**self.status(), "link": self.local_link}

    def select(self, mode, link=""):
        if mode not in {"provider", "local", "peer"}:
            raise SharingError("请选择本机、额度提供方或其他共享出口。")
        if mode == "peer" and not link:
            with self.guard:
                link = self.peer_link
        opener = self.opener_factory(link) if mode == "peer" else None
        with self.guard:
            prior = self.peer_opener
            self.mode, self.peer_link, self.peer_opener = mode, link if mode == "peer" else "", opener
        if prior:
            prior.close()
        return self.status()

    def request_exit(self, account_peer=None):
        """Return a capability for the credential holder, not a chain of exits."""
        with self.guard:
            mode, link = self.mode, self.peer_link
        if mode == "provider":
            return ""  # Explicit physical exit on the credential holder.
        if mode == "peer":
            return link
        if not account_peer:
            return ""
        with self.guard:
            if self.listener and time.time() < self.host["expires_at"]:
                return self.local_link
        # Selecting the borrower's own exit implies a callback tunnel. The
        # kernel chooses the LAN interface that can reach the quota provider.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((account_peer["host"], account_peer["port"]))
            address = route.getsockname()[0]
        return self.start(address, 0)["link"]

    def outgoing(self, override="", *, own_request=False):
        if override:
            return self.opener_factory(override, ephemeral=True)
        if own_request:
            with self.guard:
                if self.mode == "peer":
                    return self.peer_opener
        return self.server.opener

    def status(self):
        with self.guard:
            host = {key: value for key, value in self.host.items() if key not in {"grant_hash", "fingerprint"}}
            host["active"] = bool(self.listener and time.time() < self.host.get("expires_at", 0))
            selection = {"mode": self.mode, "peer_configured": bool(self.peer_link)}
            if self.peer_link:
                address, port, _token, _fingerprint = parse_egress_link(self.peer_link)
                selection.update(address=address, port=port)
            return {"supported": True, "host": host, "selection": selection}

    def close(self):
        try:
            self.stop()
        finally:
            self.select("provider")
