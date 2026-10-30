"""Opt-in, temporary LAN sharing. Native credentials never leave the owner relay."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request

from .analyzer import MAX_BODY
from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .connection_policy import codex_auth
from .sharing_auth import NativeAuthorization, SharingError
from .sharing_config import SharedProvider
from .sharing_usage import SharedUsageStream, empty_usage
from .sharing_role import MachineSharingRole
from .usage_trigger import raw_chunks

PROTOCOL = 2  # Requires the owner's private forwarding path and aggregate-only accounting.
POST_PATHS = {"/responses", "/responses/compact"}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
DROP_HEADERS = {"authorization", "proxy-authorization", "cookie", "host", "content-length",
                "chatgpt-account-id", "openai-organization", "openai-project", "api-key", "x-api-key",
                "connection", "keep-alive", "proxy-authenticate", "te", "trailer", "transfer-encoding", "upgrade",
                "forwarded", "x-real-ip", "x-inspector-control", "x-codex-inspector-packet-id"}
RESPONSE_HEADERS = {"content-type", "content-encoding", "content-length", "retry-after", "x-request-id",
                    "x-codex-turn-state", "x-codex-request-id", "openai-processing-ms"}


def allowed_request(method, suffix):
    return ((method == "POST" and suffix in POST_PATHS)
            or (method == "GET" and len(suffix) <= 2048 and urlsplit(suffix).path == "/models"))


def lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
        if address.is_loopback or any(address in network for network in PRIVATE_NETWORKS):
            return str(address)
    except (ValueError, TypeError):
        pass
    raise SharingError("请选择本机的局域网 IPv4 地址。")


def local_addresses():
    values = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            try:
                values.add(lan_address(result[4][0]))
            except SharingError:
                pass
    except OSError:
        pass
    return sorted(values - {"127.0.0.1"}) or ["127.0.0.1"]


def forwarding_headers(headers):
    connection = {v.strip().lower() for v in headers.get("Connection", "").split(",")}
    return {key: value for key, value in headers.items()
            if key.lower() not in DROP_HEADERS | connection
            and not key.lower().startswith(("x-forwarded-", "x-inspector-"))}


def parse_link(value):
    try:
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError()
        url = urlsplit(value.strip())
        if (url.scheme != "codex-share" or url.username or url.password or url.query
                or url.path not in {"", "/"} or not url.port):
            raise ValueError()
        host = lan_address(url.hostname)
        token, fingerprint = url.fragment.split(".")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError()
        return host, url.port, token, fingerprint
    except (ValueError, TypeError, SharingError):
        raise SharingError("共享链接无效，请完整复制提供方生成的链接。") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def pinned_opener(host, port, fingerprint):
    # Probe TLS without an HTTP request, grant or body. Validate the fingerprint
    # before building an opener that validates this certificate on every request.
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=8) as tcp:
        with probe.wrap_socket(tcp, server_hostname=host) as connection:
            certificate = connection.getpeercert(binary_form=True)
    if not secrets.compare_digest(hashlib.sha256(certificate).hexdigest(), fingerprint):
        raise SharingError("共享方证书不匹配，请重新获取共享链接。")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
    return build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())


def tls_context(host):
    # Lazy import: a missing optional build dependency cannot break ordinary relay startup.
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SharingError("当前安装缺少共享依赖，请更新完整安装包。") from None
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Codex LAN sharing")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="codex-share-") as folder:
        certificate, private = Path(folder) / "cert.pem", Path(folder) / "key.pem"
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        context.load_cert_chain(certificate, private)
    return context, cert.fingerprint(hashes.SHA256()).hex()


class LANServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller, context):
        self.controller, self.context = controller, context
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, LANHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(10)
            try:
                request = self.context.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                self.shutdown_request(request)
                return
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_args):
        pass  # Never echo request headers, URL grants, bodies or native secrets.


class LANHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, message):
        body = json.dumps(message, ensure_ascii=False).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def handle_route(self):
        controller = self.server.controller
        path = self.path
        suffix = path.removeprefix("/v1")
        if not path.startswith("/v1/") or (path not in {"/v1/status", "/v1/responses"}
                                           and not allowed_request(self.command, suffix)):
            self.reply(404, {"error": "sharing_route_not_found"})
            return
        if not controller.authorized(self.headers.get("Authorization", "")):
            self.reply(403, {"error": "sharing_unavailable", "message": "共享已停止、到期或链接已撤销。"})
            return
        if self.command == "GET" and path == "/v1/status":
            self.reply(200, {"protocol": PROTOCOL, "available": True})
            return
        if self.command == "GET" and path == "/v1/responses":
            self.reply(426, {"error": {"code": "websocket_not_supported", "message": "Use HTTP POST."}})
            return
        if not allowed_request(self.command, suffix):
            self.reply(405, {"error": "sharing_method_not_allowed"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0" if self.command == "GET" else "-1"))
            if size < 0 or size > MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.reply(413, {"error": "invalid_sharing_body_size"})
                return
            self.connection.settimeout(30)
            body = self.rfile.read(size)
            if len(body) != size:
                raise OSError()
        except (ValueError, OSError):
            self.reply(400, {"error": "invalid_sharing_body"})
            return
        reserved = False
        reservation = None
        sent_headers = False
        response = None
        usage = None
        try:
            headers = forwarding_headers(self.headers)
            credentials, reservation = controller.reserve(self.headers.get("Authorization", ""), count=self.command == "POST")
            headers.update(credentials)
            reserved = True
            # B has already prepared the body. A only substitutes authorization
            # and forwards, outside A's capture, diagnostics and protection paths.
            headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
            request = Request(reservation["upstream"] + suffix,
                              data=body if self.command == "POST" else None, headers=headers, method=self.command)
            try:
                response = controller.server.opener.open(request, timeout=UPSTREAM_READ_TIMEOUT_SECONDS)
            except HTTPError as exc:
                response = exc
            if self.command == "POST":
                usage = SharedUsageStream(response, lambda values: controller.add_usage(reservation, values))
            self.connection.settimeout(UPSTREAM_READ_TIMEOUT_SECONDS)
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in RESPONSE_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            for chunk in usage.chunks() if usage else raw_chunks(response):
                self.wfile.write(chunk)
                self.wfile.flush()
        except SharingError as exc:
            if not sent_headers:
                self.reply(403, {"error": "sharing_unavailable", "message": str(exc)})
        except (OSError, ValueError):
            if not sent_headers:
                self.reply(502, {"error": "sharing_relay_unreachable", "message": "提供方中转连接失败。"})
        finally:
            if response:
                response.close()
            if reserved:
                controller.release(reservation, metered=(usage.metered if usage else False)
                                   if self.command == "POST" else None)
            self.close_connection = True

    do_GET = handle_route
    do_POST = handle_route
    do_PUT = handle_route
    do_DELETE = handle_route
    do_PATCH = handle_route
    do_OPTIONS = handle_route


class Sharing:
    def __init__(self, server, *, authorization_factory=None):
        self.server = server
        self.guard = threading.RLock()
        self.authorization_factory = authorization_factory or (lambda: NativeAuthorization(native_home=server.link.config.parent))
        self.authorization = None
        self.role = MachineSharingRole()
        self.listener = None
        self.timer = None
        self.host = {}
        self.remote = None
        self.closed = False
        self.addresses = []
        self.address_time = 0
        self.provider = SharedProvider(server.link, server.data_dir / "sharing-provider.json")
        self.recovery_error = ""
        self.recovered = False
        try:
            self.recovered = self.provider.restore()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.recovery_error = "上次共享配置未能恢复，请检查 Codex 配置。"

    @property
    def client_endpoint(self):
        return self.server.endpoint + "/sharing/client"

    def _host_upstream(self, credentials):
        profile = self.server.link.profile()
        _, data = self.server.link._read()
        active = data.get("profiles", {}).get(data.get("profile"), {})
        if active.get("model_provider", profile.get("provider")) != profile.get("provider"):
            raise SharingError("当前 profile 的连接与全局连接不同，暂不支持共享此连接。")
        selected = profile.get("provider", "openai")
        provider = data.get("model_providers", {}).get(selected, {})
        if selected != "openai" and not codex_auth(provider):
            raise SharingError("当前连接不是 Codex 原生登录出口，暂不支持共享此连接。")
        upstream = profile.get("upstream", "").rstrip("/")
        expected = {"chatgpt": "https://chatgpt.com/backend-api/codex", "apikey": "https://api.openai.com/v1"}[credentials["mode"]]
        target, standard = urlsplit(upstream), urlsplit(expected)
        if (target.scheme != "https" or target.hostname != standard.hostname or target.port not in {None, 443}
                or target.path.rstrip("/") != standard.path or target.query or target.fragment
                or target.username or target.password):
            raise SharingError("当前连接不是 Codex 原生登录出口，暂不支持共享此连接。")
        return expected

    def start_host(self, address, port=47846, hours=8, request_limit=0, token_limit=0):
        address = lan_address(address)
        for value, minimum, maximum in ((port, 1, 65535), (hours, 1, 24), (request_limit, 0, 100000),
                                         (token_limit, 0, 1_000_000_000_000)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise SharingError("共享端口、时长、转发次数或 Token 上限不在允许范围。")
        with self.guard:
            if self.remote or self.listener:
                raise SharingError("请先断开或停止当前共享连接。")
            self.role.acquire()
            authorization = None
            listener = None
            try:
                authorization = self.authorization_factory()
                credentials = authorization.credentials()
                upstream = self._host_upstream(credentials)
                context, fingerprint = tls_context(address)
                listener = LANServer((address, port), self, context)
                grant = secrets.token_urlsafe(32)
                self.host = {"active": True, "address": address, "port": listener.server_address[1],
                             "expires_at": time.time() + hours * 3600, "requests": 0, "in_flight": 0,
                             "request_limit": request_limit, "token_limit": token_limit, "usage": empty_usage(),
                             "started_at": time.time(), "error": "", "upstream": upstream,
                             "grant_hash": hashlib.sha256(grant.encode()).digest(),
                             "identity_hash": hashlib.sha256(credentials["identity"].encode()).digest(),
                             "fingerprint": fingerprint}
                self.authorization, self.listener = authorization, listener
                self.recovered = False
                threading.Thread(target=listener.serve_forever, name="codex-lan-sharing", daemon=True).start()
                state = self.host
                self.timer = threading.Timer(hours * 3600, lambda: self.stop_host(expected=state))
                self.timer.daemon = True
                self.timer.start()
                return {"link": f"codex-share://{address}:{listener.server_address[1]}/#{grant}.{fingerprint}",
                        **self.status()}
            except Exception as exc:
                try:
                    if listener:
                        listener.server_close()
                    if authorization:
                        authorization.close()
                finally:
                    self.role.close()
                if isinstance(exc, SharingError):
                    raise
                raise SharingError("无法开启共享，请检查本机地址、端口及 Codex 登录状态。") from None

    def authorized(self, value):
        if not value.startswith("Bearer "):
            return False
        digest = hashlib.sha256(value[7:].encode()).digest()
        with self.guard:
            return bool(self.listener and self.host.get("active") and time.time() < self.host["expires_at"]
                        and secrets.compare_digest(digest, self.host["grant_hash"]))

    def rotate(self):
        with self.guard:
            if not self.listener or not self.host.get("active") or time.time() >= self.host["expires_at"]:
                raise SharingError("请先开启共享。")
            grant = secrets.token_urlsafe(32)
            self.host["grant_hash"] = hashlib.sha256(grant.encode()).digest()
            return {**self.status(), "link": f"codex-share://{self.host['address']}:{self.host['port']}/#{grant}.{self.host['fingerprint']}"}

    def reserve(self, value, *, count=True):
        with self.guard:
            if not self.authorized(value):
                raise SharingError("共享已停止、到期或链接已撤销。")
            limit = self.host["request_limit"]
            if count and limit and self.host["requests"] >= limit:
                raise SharingError("本次共享的转发次数已用完。")
            if count and self.host["token_limit"] and self.host["usage"]["total_tokens"] >= self.host["token_limit"]:
                raise SharingError("本次共享的 Token 上限已达到。")
            credentials = self.authorization.credentials()
            if not self.authorized(value):
                raise SharingError("共享已停止、到期或链接已撤销。")
            if not secrets.compare_digest(hashlib.sha256(credentials["identity"].encode()).digest(), self.host["identity_hash"]):
                self.host.update(active=False, error="Codex 登录已变更，请重新开启共享。")
                raise SharingError(self.host["error"])
            self.host["requests"] += int(count)
            self.host["in_flight"] += 1
            return credentials["headers"], self.host

    def add_usage(self, reservation, values):
        with self.guard:
            usage = reservation["usage"]
            for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
                usage[key] += values.get(key, 0)
            usage["responses"] += 1
            usage["cached_responses"] += int("cached_input_tokens" in values)

    def release(self, reservation, *, metered=None):
        with self.guard:
            reservation["in_flight"] = max(0, reservation.get("in_flight", 0) - 1)
            if metered is False:
                reservation["usage"]["missing_responses"] += 1

    def stop_host(self, *, expected=None):
        with self.guard:
            if expected is not None and self.host is not expected:
                return self.status()
            listener, self.listener = self.listener, None
            authorization, self.authorization = self.authorization, None
            timer, self.timer = self.timer, None
            self.host["active"] = False
            self.host.pop("grant_hash", None)
            self.host.pop("identity_hash", None)
            if listener:
                self.role.close()
        if timer:
            timer.cancel()
        if listener:
            listener.shutdown()
            listener.server_close()
        if authorization:
            authorization.close()
        return self.status()

    def connect(self, link):
        with self.guard:
            if self.listener or self.remote:
                raise SharingError("请先断开或停止当前共享连接。")
            host, port, token, fingerprint = parse_link(link)
            self.role.acquire()
            try:
                opener = pinned_opener(host, port, fingerprint)
                upstream = f"https://{host}:{port}/v1"
                request = Request(upstream + "/status", headers={"Authorization": "Bearer " + token})
                with opener.open(request, timeout=8) as response:
                    result = json.loads(response.read(4096))
                if result.get("protocol") != PROTOCOL or result.get("available") is not True:
                    raise SharingError("共享协议不兼容，请双方更新软件。")
                settings = self.server.settings
                self.provider.activate(self.client_endpoint, settings["capacity_retry_max_retries"]
                                       if settings["capacity_retry_enabled"] else None)
                self.remote = {"host": host, "port": port, "upstream": upstream, "opener": opener, "token": token}
                self.recovered = False
                self.server.connection_reload_required = True
                return {**self.status(), "reload_required": True}
            except SharingError:
                raise
            except (OSError, ValueError):
                raise SharingError("无法连接共享，请检查链接是否有效、双方网络及提供方防火墙。") from None
            finally:
                if self.remote is None:
                    self.role.close()

    def disconnect(self):
        with self.guard:
            borrowing = self.remote is not None
            had_remote = self.remote is not None or self.provider.path.exists()
            self.provider.restore()
            self.remote = None
            self.recovered = False
            if borrowing:
                self.role.close()
            if had_remote:
                self.server.connection_reload_required = True
            return {**self.status(), "reload_required": had_remote}

    def client_route(self):
        with self.guard:
            if not self.remote:
                raise SharingError("共享连接已断开，请重新连接。")
            return {**self.remote}

    def status(self):
        with self.guard:
            if not self.closed and (not self.address_time or time.monotonic() - self.address_time > 30):
                self.addresses, self.address_time = local_addresses(), time.monotonic()
            keys = ("active", "address", "port", "started_at", "expires_at", "requests", "in_flight",
                    "request_limit", "token_limit", "error")
            host = {key: self.host[key] for key in keys if key in self.host}
            host["usage"] = dict(self.host.get("usage", empty_usage()))
            host["active"] = bool(self.listener and self.host.get("active") and time.time() < self.host.get("expires_at", 0))
            host["running"] = bool(self.listener)
            client = {"active": bool(self.remote)}
            if self.remote:
                client.update(address=self.remote["host"], port=self.remote["port"])
            return {"supported": True, "protocol": PROTOCOL, "host": host, "client": client, "addresses": self.addresses,
                    "recovery_error": self.recovery_error, "recovered": self.recovered}

    def close(self):
        self.closed = True
        try:
            self.stop_host()
        finally:
            try:
                self.disconnect()
            finally:
                self.role.close()
