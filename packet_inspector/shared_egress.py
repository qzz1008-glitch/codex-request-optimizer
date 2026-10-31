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

from .egress_transport import EGRESS_PROTOCOL, MISSING_EXIT_ROUTE_MESSAGE, ExitOpener, UPSTREAM_HOSTS, parse_egress_link
from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .sharing import LANServer, SharingError, automatic_address, lan_address, tls_context
from .sharing_credentials import CLIENT_HEADER, ExitMachineMismatch


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
        else:
            controller = self.server.controller
            try:
                self.reply(200, controller.peer_status(self.headers.get("Authorization", ""), self.headers.get(CLIENT_HEADER, "")))
            except ExitMachineMismatch:
                self.reply(403, {"error": "exit_credential_machine_mismatch"})
            except SharingError:
                self.reply(403, {"error": "exit_unavailable"})

    def do_CONNECT(self):
        controller = self.server.controller
        if self.path not in {host + ":443" for host in UPSTREAM_HOSTS}:
            self.reply(403, {"error": "exit_target_forbidden"})
            return
        try:
            reservation = controller.reserve(self.headers.get("Proxy-Authorization", ""), self.headers.get(CLIENT_HEADER, ""))
        except SharingError:
            reservation = None
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
        self.mode = "local"
        self.peer_link = ""
        self.peer_opener = None
        self.peer_id = None
        self.callback_link = ""
        self.connect_physical = lambda host, port: physical_tunnel(server, host, port)
        self.opener_factory = ExitOpener

    def _stop_timer(self):
        timer, self.timer = self.timer, None
        if timer:
            timer.cancel()

    @property
    def vault(self):
        return self.server.sharing.vault

    def start(self, address=None, port=None, hours=24):
        if type(hours) is not int or hours != 24:
            raise SharingError("出口时长不在允许范围。")
        with self.guard:
            if self.listener and self.local_link:
                return {**self.status(), "link": self.local_link}
        now = time.time()
        return self.issue(address=address, port=port, expires_at=now + hours * 3600, issued_at=now)

    def ensure_service(self, address=None, port=None):
        with self.guard:
            if self.listener:
                return
        address = automatic_address() if address is None else lan_address(address)
        port = self.server.settings["egress_port"] if port is None else port
        if type(port) is not int or not 0 <= port <= 65535:
            raise SharingError("出口端口不在允许范围。")
        with self.guard:
            if self.listener:
                return
            context, fingerprint = tls_context(address, self.vault, "exit")
            try:
                listener = LANServer((address, port), self, context, ExitHandler)
            except OSError:
                raise SharingError("无法开启出口共享，请检查网络或设置中的出口端口。") from None
            self.host = {"address": address, "port": listener.server_port, "expires_at": None,
                         "fingerprint": fingerprint,
                         "connections": 0, "in_flight": 0, "sent_bytes": 0, "received_bytes": 0}
            self.listener = listener
            threading.Thread(target=listener.serve_forever, name="codex-shared-exit", daemon=True).start()

    def issue(self, *, label="", expires_at=None, address=None, port=None, internal=False, issued_at=None):
        from .sharing_credentials import expiration
        issued_at = time.time() if issued_at is None else issued_at
        expiration(expires_at, since=issued_at)
        if not internal:
            label = self.vault.prepare_label("exit", label)
        self.ensure_service(address, port)
        with self.guard:
            identifier, _ = self.vault.issue("exit", "本机出口回接" if internal else label, expires_at, internal=internal, issued_at=issued_at)
            if not internal:
                self.host["last_credential_id"] = identifier
                self.local_link = self.credential_link(identifier)
            return {**self.status(), "link": self.credential_link(identifier), "issued_id": identifier}

    def credential_link(self, identifier):
        if not self.listener:
            raise SharingError("请先开启出口借出。")
        token = self.vault.get_secret(identifier, "exit", "out")
        return f"codex-exit://{self.host['address']}:{self.host['port']}/#{token}.{self.host['fingerprint']}"

    def authorized(self, value, client=""):
        with self.guard:
            if not self.listener or (self.host.get("expires_at") is not None and time.time() >= self.host["expires_at"]):
                return False
            try:
                self.vault.authenticate("exit", value, client)
                return True
            except SharingError:
                return False

    def peer_status(self, value, client):
        with self.guard:
            if not self.listener or (self.host.get("expires_at") is not None and time.time() >= self.host["expires_at"]):
                raise SharingError("出口共享已停止或到期。")
            reservation = self.vault.authenticate("exit", value, client)
            return {"kind": "network_exit", "protocol": EGRESS_PROTOCOL, "credential": self.vault.snapshot(reservation)}

    def reserve(self, value, client=""):
        with self.guard:
            if not self.authorized(value, client):
                return None
            reservation = self.vault.authenticate("exit", value, client, spend=True)
            self.host["connections"] += 1
            self.host["in_flight"] += 1
            return {**reservation, "state": self.host}

    def release(self, reservation, sent, received):
        with self.guard:
            state = reservation["state"]
            state["in_flight"] -= 1
            state["sent_bytes"] += sent
            state["received_bytes"] += received
            self.vault.record(reservation, sent=sent, received=received)

    def stop(self, *, expected=None):
        with self.guard:
            if expected is not None and self.host is not expected:
                return self.status()
            listener, self.listener = self.listener, None
            self._stop_timer()
            self.local_link = ""
            self.callback_link = ""
            self.host.pop("grant_hash", None)
        if listener:
            listener.shutdown()
            listener.server_close()
        return self.status()

    def rotate(self):
        with self.guard:
            if not self.listener:
                raise SharingError("请先开启共享出口。")
            prior = self.host.get("last_credential_id")
            if prior:
                self.vault.update(prior, {"enabled": False})
            return self.issue()

    def select(self, mode, link="", *, label="", identifier=None):
        if mode == "provider":  # Retired clients migrate to their own exit.
            mode = "local"
        if mode not in {"local", "peer"}:
            raise SharingError("请选择本机出口或借入出口。")
        if identifier:
            link = self.vault.get_secret(identifier, "exit", "in")
        if mode == "peer" and not link:
            with self.guard:
                link = self.peer_link
            if not link:
                mode = "local"
        opener = self.opener_factory(link, client_id=self.vault.client_id) if mode == "peer" else None
        try:
            saved_id = self.vault.import_link("exit", link, label, getattr(opener, "snapshot", {})) if opener else None
        except Exception:
            if opener:
                opener.close()
            raise
        with self.guard:
            prior = self.peer_opener
            self.mode, self.peer_link, self.peer_opener = mode, link if mode == "peer" else "", opener
            self.peer_id = saved_id
        if prior:
            prior.close()
        return self.status()

    def request_exit(self, account_peer=None):
        """Return a capability for the credential holder, not a chain of exits."""
        with self.guard:
            mode, link = self.mode, self.peer_link
        if mode == "peer" and link:
            return link
        if not account_peer:
            return ""
        with self.guard:
            if self.listener and self.callback_link:
                return self.callback_link
        # Selecting the borrower's own exit implies a callback tunnel. The
        # kernel chooses the LAN interface that can reach the quota provider.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((account_peer["host"], account_peer["port"]))
            address = route.getsockname()[0]
        with self.guard:
            if not self.listener or not self.callback_link:
                self.callback_link = self.issue(address=address, internal=True)["link"]
            return self.callback_link

    def outgoing(self, override="", *, own_request=False, client_id=None):
        if override:
            return self.opener_factory(override, client_id=client_id or self.vault.client_id, ephemeral=True)
        if own_request:
            with self.guard:
                if self.mode == "peer" and self.peer_link:
                    if self.peer_opener is None:
                        raise SharingError("借入出口未就绪，请重新借入出口或选择本机出口。")
                    return self.peer_opener
            return self.server.opener
        raise SharingError(MISSING_EXIT_ROUTE_MESSAGE, code="sharing_exit_metadata_missing")

    def status(self):
        with self.guard:
            host = {key: value for key, value in self.host.items() if key not in {"grant_hash", "fingerprint"}}
            host["active"] = bool(self.listener and (self.host.get("expires_at") is None or time.time() < self.host["expires_at"]))
            mode = "peer" if self.mode == "peer" and self.peer_link else "local"
            selection = {"mode": mode, "peer_configured": bool(self.peer_link), "active_id": self.peer_id if mode == "peer" else None}
            if self.peer_link:
                address, port, _token, _fingerprint = parse_egress_link(self.peer_link)
                selection.update(address=address, port=port)
            return {"supported": True, "host": host, "selection": selection}

    def close(self):
        try:
            self.stop()
        finally:
            self.select("local")
