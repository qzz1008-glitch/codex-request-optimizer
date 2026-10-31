"""Pinned HTTPS CONNECT, with independent end-to-end upstream TLS."""
import json
import ssl
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, ProxyHandler, Request

from .http_io import UPSTREAM_READ_TIMEOUT_SECONDS
from .sharing import NoRedirect, parse_link, pinned_tls_context
from .sharing_auth import SharingError
from .sharing_credentials import CLIENT_HEADER

EGRESS_HEADER = "X-Inspector-Shared-Egress"
EGRESS_PROTOCOL = 2
MISSING_EXIT_ROUTE_MESSAGE = "共享出口信息不完整，请更新借用方软件后重试。"
UPSTREAM_HOSTS = frozenset({"chatgpt.com", "api.openai.com"})


def parse_egress_link(value):
    if not isinstance(value, str) or not value.strip().startswith("codex-exit://"):
        raise SharingError("出口链接无效，请完整复制共享出口链接。")
    try:
        return parse_link("codex-share://" + value.strip()[len("codex-exit://"):])
    except SharingError:
        raise SharingError("出口链接无效，请完整复制共享出口链接。") from None


class ExitResponse:
    def __init__(self, response, close=None):
        self.response, self.after_close = response, close
        self.status, self.reason, self.headers = response.status, response.reason, response.headers

    def read1(self, size=65536):
        try:
            return self.response.read1(size, decode_content=False)
        except Exception:
            raise OSError("共享出口响应中断。") from None

    def read(self, size=-1):
        try:
            return self.response.read(None if size < 0 else size, decode_content=False)
        except Exception:
            raise OSError("共享出口响应中断。") from None

    def close(self):
        try:
            self.response.close()
            self.response.release_conn()
        finally:
            if self.after_close:
                close, self.after_close = self.after_close, None
                close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ExitOpener:
    def __init__(self, link, *, client_id, ephemeral=False, target_context=None):
        # Import lazily so an unavailable optional dependency cannot affect the
        # original relay, account-sharing tab or an exit-only provider.
        try:
            import urllib3
            if tuple(int(part) for part in urllib3.__version__.split(".")[:2]) < (2, 8):
                raise ImportError()
        except (ImportError, ValueError):
            raise SharingError("当前安装缺少共享出口依赖，请更新完整安装包。") from None
        host, port, token, fingerprint = parse_egress_link(link)
        try:
            context = pinned_tls_context(host, port, fingerprint)
            probe = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
            with probe.open(Request(f"https://{host}:{port}/v1/status",
                                    headers={"Authorization": "Bearer " + token, CLIENT_HEADER: client_id}), timeout=8) as response:
                value = json.loads(response.read(256_000))
            if value.get("protocol") != EGRESS_PROTOCOL or value.get("kind") != "network_exit":
                raise SharingError("共享出口协议不兼容，请双方更新软件。")
            self.manager = urllib3.ProxyManager(f"https://{host}:{port}",
                proxy_headers={"Proxy-Authorization": "Bearer " + token, CLIENT_HEADER: client_id}, proxy_ssl_context=context,
                proxy_assert_fingerprint=fingerprint, use_forwarding_for_https=False,
                ssl_context=target_context or ssl.create_default_context(), retries=False,
                maxsize=8, num_pools=2)
        except HTTPError as exc:
            with exc:
                try:
                    code = json.loads(exc.read(4096)).get("error") if exc.code == 403 else ""
                except (ValueError, AttributeError, OSError):
                    code = ""
            if code == "exit_credential_machine_mismatch":
                raise SharingError("此出口凭据已绑定其他机器，请使用独立凭据。") from None
            raise SharingError("无法连接共享出口，请检查链接、网络和防火墙。") from None
        except SharingError:
            raise
        except Exception:
            raise SharingError("无法连接共享出口，请检查链接、网络和防火墙。") from None
        self.ephemeral = ephemeral
        self.snapshot = value.get("credential", {})
        self.status_url = f"https://{host}:{port}/v1/status"
        self.status_opener, self.token, self.client_id = probe, token, client_id

    def refresh_status(self):
        import json
        with self.status_opener.open(Request(self.status_url, headers={"Authorization": "Bearer " + self.token,
                                                                      CLIENT_HEADER: self.client_id}), timeout=5) as response:
            value = json.loads(response.read(256_000))
        self.snapshot = value.get("credential", {})
        return self.snapshot

    def open(self, request, timeout=UPSTREAM_READ_TIMEOUT_SECONDS):
        target = urlsplit(request.full_url)
        if target.scheme != "https" or target.hostname not in UPSTREAM_HOSTS or target.port not in {None, 443} or target.username or target.password:
            raise SharingError("共享出口仅支持 Codex 官方 HTTPS 上游。")
        try:
            import urllib3
            headers = {key: value for key, value in request.header_items()
                       if key.lower() not in {"proxy-authorization", EGRESS_HEADER.lower(), CLIENT_HEADER.lower()}}
            headers["Connection"] = "close"
            response = self.manager.urlopen(request.get_method(), request.full_url, body=request.data,
                headers=headers, redirect=False, retries=False, preload_content=False,
                decode_content=False, timeout=urllib3.Timeout(connect=8, read=timeout))
            return ExitResponse(response, self.close if self.ephemeral else None)
        except Exception:
            if self.ephemeral:
                self.close()
            raise OSError("共享出口连接失败。") from None

    def close(self):
        self.manager.clear()
