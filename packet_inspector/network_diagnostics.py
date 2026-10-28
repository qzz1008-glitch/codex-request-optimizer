"""Connection-time checks and bounded failure diagnostics; never replay requests."""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import hashlib
from html.parser import HTMLParser
import http.client
import io
import json
import os
from pathlib import Path
import platform
import re
import socket
import sqlite3
import ssl
import threading
import time
import traceback
from urllib import request as urllib_request
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPSHandler
import uuid
import zlib

from . import VERSION
from .analyzer import clean_text, redact
from .build_info import build_info

RETENTION_SECONDS = 14 * 86400
MAX_RECORDS = 500
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_ERROR_BYTES = 128 * 1024
MAX_ERROR_TEXT = 24000
ACTIVE_TRACE = ContextVar("inspector_network_trace", default=None)
DNS_SLOTS = threading.BoundedSemaphore(3)
RESPONSE_HEADERS = {"content-type", "content-encoding", "content-length", "server", "via", "date",
                    "retry-after", "x-request-id", "x-openai-request-id", "cf-ray", "x-cache",
                    "x-cache-lookup", "x-squid-error", "x-proxy-error", "x-error-code", "location"}


def safe_url(value):
    """Keep the destination, never proxy passwords, query values or relay tokens."""
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
        if not host:
            return ""
        host = "[" + host + "]" if ":" in host else host
        netloc = host + (":" + str(parsed.port) if parsed.port else "")
        path = re.sub(r"/relay/[^/]+", "/relay/[redacted]", parsed.path)
        return urlunsplit((parsed.scheme, netloc, path, "[redacted]" if parsed.query else "", ""))[:1500]
    except (ValueError, TypeError):
        return "[invalid URL]"


def safe_proxy(value):
    return safe_url(value if "://" in str(value) else "http://" + str(value))


def safe_text(value, secrets=(), limit=MAX_ERROR_TEXT):
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[credential redacted]")
    text = clean_text(text)
    text = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [credential redacted]", text, flags=re.I)
    text = re.sub(r"\b(?:https?|socks\w*)://[^\s<>\"']+", lambda m: safe_url(m[0]), text)
    text = re.sub(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]+",
                  r"\1: [credential redacted]", text)
    text = re.sub(r"(?i)([\"']?(?:password|access_token|refresh_token|api_key|client_secret)[\"']?\s*[:=]\s*)[\"']?[^\s,;<>]+",
                  r"\1[credential redacted]", text)
    return text[:limit]


def exception_detail(exc, secrets=()):
    result = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(result) < 4:
        seen.add(id(exc))
        result.append({"type": type(exc).__name__, "message": safe_text(exc, secrets, 2000),
                       "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                                  for frame in traceback.extract_tb(exc.__traceback__)[-8:]],
                       **({"errno": exc.errno} if isinstance(getattr(exc, "errno", None), int) else {}),
                       **({"winerror": exc.winerror} if isinstance(getattr(exc, "winerror", None), int) else {}),
                       **({"verify_code": exc.verify_code, "verify_message": safe_text(exc.verify_message, secrets, 500)}
                          if isinstance(exc, ssl.SSLCertVerificationError) else {})})
        exc = getattr(exc, "reason", None) if isinstance(getattr(exc, "reason", None), BaseException) else exc.__cause__ or exc.__context__
    return result


def resolve_probe(host, port, timeout):
    """Bound DNS observation without changing the resolver used by requests."""
    if not DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError("Previous diagnostic DNS checks are still running")
    result, done = {}, threading.Event()
    def resolve():
        try:
            result["addresses"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            result["error"] = exc
        finally:
            DNS_SLOTS.release()
            done.set()
    threading.Thread(target=resolve, daemon=True, name="diagnostic-dns").start()
    if not done.wait(max(0, timeout)):
        raise TimeoutError("Diagnostic DNS check exceeded its time budget")
    if "error" in result:
        raise result["error"]
    return result["addresses"]


def windows_proxy_settings():
    result = {"available": os.name == "nt"}
    if os.name != "nt":
        return result
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            for field in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect"):
                try:
                    value = winreg.QueryValueEx(key, field)[0]
                except OSError:
                    continue
                if field == "ProxyServer":
                    result[field] = [safe_proxy(item.split("=", 1)[-1]) for item in str(value).split(";")[:12]]
                elif field == "AutoConfigURL":
                    result[field] = safe_url(value)
                    result["pac_configured"] = bool(value)
                elif field == "ProxyOverride":
                    result[field] = safe_text(value, limit=2000)
                else:
                    result[field] = bool(value)
    except OSError as exc:
        result["read_error"] = type(exc).__name__
    # Windows stores automatic detection flags outside the simple ProxyServer
    # registry values. This API reads that configuration without fetching a PAC.
    try:
        import ctypes
        from ctypes import wintypes
        class ProxyConfig(ctypes.Structure):
            _fields_ = [("auto_detect", wintypes.BOOL), ("pac", ctypes.c_void_p),
                        ("proxy", ctypes.c_void_p), ("bypass", ctypes.c_void_p)]
        config = ProxyConfig()
        query = ctypes.WinDLL("winhttp", use_last_error=True).WinHttpGetIEProxyConfigForCurrentUser
        query.argtypes, query.restype = [ctypes.POINTER(ProxyConfig)], wintypes.BOOL
        free = ctypes.WinDLL("kernel32").GlobalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        try:
            if query(ctypes.byref(config)):
                result["windows_auto_detect"] = bool(config.auto_detect)
                result["windows_pac_configured"] = bool(config.pac)
                if config.pac:
                    result["windows_pac_url"] = safe_url(ctypes.wstring_at(config.pac))
            else:
                result["windows_auto_config_error"] = ctypes.get_last_error()
        finally:
            for pointer in (config.pac, config.proxy, config.bypass):
                if pointer:
                    free(pointer)
    except Exception as exc:
        result["windows_auto_config_error"] = type(exc).__name__
    return result


def environment_snapshot(proxies):
    environment = urllib_request.getproxies_environment()
    registry = getattr(urllib_request, "getproxies_registry", lambda: {})()
    current = urllib_request.getproxies()
    if not (set(current) - {"no"}):
        current = {**registry, **current}
    source = "environment" if set(environment) - {"no"} else "windows_registry" if set(registry) - {"no"} else "direct"
    def describe(values):
        return {key: safe_text(value, limit=2000) if key == "no" else safe_proxy(value)
                for key, value in list(values.items())[:12]}
    certificates = {}
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "CODEX_CA_CERTIFICATE", "NODE_EXTRA_CA_CERTS"):
        if os.environ.get(key):
            certificates[key] = {"configured": True, "exists": Path(os.environ[key]).exists()}
    return {"application_version": VERSION, "os": platform.system(), "os_release": platform.release(),
            "build": build_info(),
            "architecture": platform.machine(), "python_version": platform.python_version(),
            "openssl": ssl.OPENSSL_VERSION, "proxy_configuration_source_now": source,
            "environment_proxies": describe(environment), "windows_proxy": windows_proxy_settings(),
            "opener_proxies": describe(proxies), "proxy_settings_changed_since_opener_created": current != proxies,
            "pac_supported_by_transport": False, "all_proxy_supported_by_transport": False,
            "certificate_environment": certificates,
            "transport": "urllib HTTP/1.1; redirects disabled; operating-system/environment proxy selection"}


class _ErrorHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden and value.strip():
            self.parts.append(value.strip())


def error_body(raw, headers, secrets=(), truncated=False):
    encoding = headers.get("content-encoding", "identity").lower()
    try:
        if encoding == "gzip":
            raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, MAX_ERROR_BYTES + 1)
        elif encoding == "deflate":
            raw = zlib.decompressobj().decompress(raw, MAX_ERROR_BYTES + 1)
        elif encoding == "zstd":
            import zstandard
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                raw = reader.read(MAX_ERROR_BYTES + 1)
        elif encoding not in {"", "identity"}:
            return {"decode_error": "unsupported_content_encoding", "encoding": encoding}
        truncated |= len(raw) > MAX_ERROR_BYTES
        kind = headers.get("content-type", "").lower()
        charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", kind)
        try:
            text = raw[:MAX_ERROR_BYTES].decode(charset[1] if charset else "utf-8", "replace")
        except LookupError:
            text = raw[:MAX_ERROR_BYTES].decode("utf-8", "replace")
        if "html" in kind or text.lstrip().lower().startswith(("<!doctype html", "<html")):
            parser = _ErrorHTML()
            parser.feed(text)
            text = "\n".join(parser.parts)
            form = "html_text"
        else:
            form = "text"
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    value = value.get("error", {k: value[k] for k in ("code", "type", "message", "detail") if k in value})
                    text = json.dumps(redact(value), ensure_ascii=False)
                    form = "json_error"
            except (TypeError, ValueError):
                pass
        result = safe_text(text, secrets)
        return {"format": form, "text": result, "truncated": truncated or len(text) > MAX_ERROR_TEXT}
    except Exception as exc:
        return {"decode_error": type(exc).__name__, "encoding": encoding, "captured_bytes": len(raw)}


class NetworkDiagnostics:
    def __init__(self, directory, proxies):
        self.path = Path(directory) / "network-diagnostics.sqlite"
        self.proxies = dict(proxies)
        self.lock = threading.RLock()
        self.connection_id = ""
        self.errors = 0
        self.last_error = ""
        self.probing = False

    def _save(self, record):
        try:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_RECORD_BYTES:
                record = {**record, "error_body": {"truncated": True, "text": record.get("error_body", {}).get("text", "")[:6000]}}
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_RECORD_BYTES:
                raise ValueError("diagnostic record exceeds limit")
            with self.lock, closing(sqlite3.connect(self.path, timeout=.2)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS diagnostics (id TEXT PRIMARY KEY, created REAL, size INTEGER, document TEXT)")
                db.execute("INSERT OR REPLACE INTO diagnostics VALUES (?,?,?,?)",
                           (record["id"], record["created"], len(encoded), encoded.decode("utf-8")))
                db.execute("DELETE FROM diagnostics WHERE created < ?", (time.time() - RETENTION_SECONDS,))
                rows = db.execute("SELECT id,size FROM diagnostics ORDER BY created DESC,id DESC").fetchall()
                size, remove = 0, []
                for position, (identifier, amount) in enumerate(rows):
                    size += amount
                    if position >= MAX_RECORDS or size > MAX_BYTES:
                        remove.append((identifier,))
                db.executemany("DELETE FROM diagnostics WHERE id=?", remove)
        except Exception as exc:
            self.errors += 1
            self.last_error = type(exc).__name__ + ": " + safe_text(exc, limit=300)

    def export(self):
        records = []
        try:
            if self.path.exists():
                with self.lock, closing(sqlite3.connect(self.path, timeout=.2)) as db:
                    records = [json.loads(row[0]) for row in db.execute(
                        "SELECT document FROM diagnostics WHERE created >= ? ORDER BY created DESC,id DESC LIMIT ?",
                        (time.time() - RETENTION_SECONDS, MAX_RECORDS))]
        except Exception:
            self.errors += 1
        return {"version": 1, "application_version": VERSION, "records": records, "write_errors": self.errors,
                "last_write_error": self.last_error,
                "retention_days": 14, "max_records": MAX_RECORDS, "max_bytes": MAX_BYTES}

    def connection(self, before, after, routes, settings, result=None, error=None):
        try:
            identifier = uuid.uuid4().hex
            self.connection_id = identifier
            profile = lambda p: {k: safe_url(p[k]) if k in {"base_url", "upstream"} else p[k]
                                 for k in ("provider", "active", "base_url", "upstream", "read_error", "connection_settings") if k in p}
            destinations = [{"provider": str(route.get("provider", ""))[:160], "upstream": safe_url(route.get("upstream", ""))}
                            for route in list(routes.values())[:32]]
            record = {"id": identifier, "created": time.time(), "kind": "connection", "trigger": "user_connect",
                      "process_id": os.getpid(),
                      "outcome": "failed" if error else "connected", "before": profile(before), "after": profile(after),
                      "routes": destinations, "environment": environment_snapshot(self.proxies),
                      "route_count": len(routes), "routes_truncated": len(routes) > len(destinations),
                      "settings": {k: settings.get(k) for k in ("zstd_level", "packet_size_protection_enabled", "budget_bytes",
                                    "capacity_retry_enabled", "image_analysis_mode")},
                      "configuration_changed": bool((result or {}).get("changed")),
                      "probe_status": "not_started" if error else "running"}
            if error:
                record["exception"] = exception_detail(error)
            self._save(record)
            if not error:
                with self.lock:
                    if self.probing:
                        record["probe_status"] = "skipped_previous_probe_running"
                        self._save(record)
                        return
                    self.probing = True
                threading.Thread(target=self._probe, args=(record,), daemon=True, name="connection-diagnostics").start()
        except Exception:
            self.errors += 1

    def _probe(self, record):
        results = []
        deadline = time.monotonic() + 8
        seen = set()
        try:
            for route in record["routes"]:
                destination = urlsplit(route["upstream"])
                proxy = self.proxies.get(destination.scheme)
                bypass = destination.hostname in {"localhost", "127.0.0.1", "::1"} or urllib_request.proxy_bypass(destination.netloc)
                endpoint = urlsplit(safe_proxy(proxy)) if proxy and not bypass else destination
                host, port = endpoint.hostname, endpoint.port or (443 if endpoint.scheme == "https" else 80)
                if not host or (host, port) in seen or len(seen) >= 3 or time.monotonic() >= deadline:
                    continue
                seen.add((host, port))
                item = {"host": host, "port": port, "role": "proxy" if proxy and not bypass else "upstream",
                        "upstream": route["upstream"], "proxy_bypassed": bypass}
                start = time.monotonic()
                try:
                    addresses = resolve_probe(host, port, min(2, deadline - time.monotonic()))
                    item["dns_ms"] = round((time.monotonic() - start) * 1000, 1)
                    item["resolved_ips"] = sorted({a[4][0] for a in addresses})[:8]
                    last_error = None
                    for family, socktype, protocol, _, address in addresses[:3]:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            connect_started = time.monotonic()
                            with socket.socket(family, socktype, protocol) as sock:
                                sock.settimeout(min(2, remaining))
                                sock.connect(address)
                                item["tcp_ms"] = round((time.monotonic() - connect_started) * 1000, 1)
                                item["peer_ip"] = sock.getpeername()[0]
                            item["outcome"] = "tcp_connected"
                            break
                        except OSError as exc:
                            last_error = exc
                    else:
                        item["exception"] = exception_detail(last_error)
                    if "outcome" not in item:
                        item["outcome"] = "failed_or_deadline"
                except Exception as exc:
                    item.update(outcome="dns_failed", exception=exception_detail(exc))
                item["total_ms"] = round((time.monotonic() - start) * 1000, 1)
                results.append(item)
            record.update(probe_status="completed", probes=results)
            self._save(record)
        except Exception:
            self.errors += 1
        finally:
            with self.lock:
                self.probing = False

    def request(self, upstream, method, headers, body, *, packet_id=None, session_id=None, kind="responses", **sizes):
        return RequestTrace(self, upstream, method, headers, body, packet_id, session_id, kind, sizes)

    def local_error(self, upstream, method, headers, body, exc, status, packet_id=None):
        try:
            trace = self.request(upstream, method, headers, body, packet_id=packet_id)
            trace.record.update(kind="local_request_error", status=status, request_sent=False, phase="request_preparation")
            trace.finish(exc)
        except Exception:
            self.errors += 1


class RequestTrace:
    def __init__(self, journal, upstream, method, headers, body, packet_id, session_id, kind, sizes):
        self.journal, self.body = journal, body
        self.start = time.monotonic()
        self.secrets = [str(v) for k, v in headers.items() if k.lower() in {
            "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"} and v]
        self.secrets.extend(value.split(" ", 1)[1] for value in list(self.secrets) if " " in value)
        for value in journal.proxies.values():
            try:
                p = urlsplit(value if "://" in value else "http://" + value)
                self.secrets.extend(unquote(v) for v in (p.username, p.password) if v)
            except ValueError:
                pass
        self.record = {"id": uuid.uuid4().hex, "created": time.time(), "kind": "request_error",
                       "process_id": os.getpid(),
                       "connection_id": journal.connection_id, "packet_id": packet_id, "session_id": session_id,
                       "request_kind": kind, "method": method, "upstream": safe_url(upstream),
                       "proxy_policy": "direct_for_loopback" if urlsplit(upstream).hostname in {"localhost", "127.0.0.1", "::1"} else "opener_proxies",
                       "flow": {"prepared_body_bytes": len(body), **sizes}, "timings_ms": {},
                       "request_headers": {k.lower(): safe_text(v, self.secrets, 500) for k, v in headers.items()
                                           if k.lower() in {"content-type", "content-encoding", "accept", "accept-encoding", "user-agent"}},
                       "phase": "prepare", "response_headers": {}}
        self.failed = False
        self.capture_http_body = False
        self.finished = False
        self.captured = bytearray()
        self.response_bytes = 0
        self.forwarded_bytes = 0

    @contextmanager
    def active(self):
        token = ACTIVE_TRACE.set(self)
        try:
            yield
        finally:
            ACTIVE_TRACE.reset(token)

    def response(self, response):
        self.record["status"] = response.status
        self.record["response_headers"] = {k.lower(): safe_text(v, self.secrets, 2000)
                                            for k, v in response.headers.items() if k.lower() in RESPONSE_HEADERS}
        self.record["timings_ms"]["headers_from_request_start"] = round((time.monotonic() - self.start) * 1000, 1)
        self.failed |= response.status >= 300
        self.capture_http_body = response.status >= 300
        if self.failed:
            self.record["failure_phase"] = "upstream_http_response"
        content_type = self.record["response_headers"].get("content-type", "").split(";", 1)[0].lower()
        if self.record["method"] == "POST" and response.status == 200 and content_type and content_type not in {
                "text/event-stream", "application/json"} and not content_type.endswith("+json"):
            self.failed = True
            self.capture_http_body = True
            self.record["protocol_error"] = "unexpected_response_content_type"
            self.record["failure_phase"] = "response_protocol"
        self.record["phase"] = "response_body"

    def feed(self, chunk):
        if chunk and not self.response_bytes:
            self.record["timings_ms"]["first_body_byte"] = round((time.monotonic() - self.start) * 1000, 1)
        self.response_bytes += len(chunk)
        if self.capture_http_body and len(self.captured) < MAX_ERROR_BYTES:
            self.captured.extend(chunk[:MAX_ERROR_BYTES - len(self.captured)])

    def stream_error(self, payload):
        self.failed = True
        self.record["failure_phase"] = "upstream_stream"
        response = payload.get("response", {})
        error = response.get("error") if isinstance(response, dict) else None
        error = error or payload.get("error") or {k: payload[k] for k in ("code", "type", "message") if k in payload}
        self.record["stream_error"] = safe_text(json.dumps(redact(error), ensure_ascii=False), self.secrets, 12000)
        self.record["stream_event"] = str(payload.get("type", ""))[:160]
        if isinstance(response, dict) and response.get("incomplete_details"):
            self.record["incomplete_details"] = safe_text(json.dumps(redact(response["incomplete_details"]), ensure_ascii=False), self.secrets, 2000)

    def finish(self, exc=None, *, phase=None):
        if self.finished:
            return
        self.finished = True
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)) and phase == "downstream_write" and not self.failed:
            return  # A client cancellation after successful output is not an upstream error.
        if not (self.failed or exc):
            return
        try:
            if exc:
                self.record["exception"] = exception_detail(exc, self.secrets)
                self.record["failure_phase"] = (phase if phase in {"upstream_read", "downstream_write"}
                                                else self.record.get("failure_phase", self.record["phase"]))
                self.record["relay_phase"] = phase
            self.record["timings_ms"]["total"] = round((time.monotonic() - self.start) * 1000, 1)
            self.record["flow"].update(response_body_bytes=self.response_bytes, forwarded_body_bytes=self.forwarded_bytes,
                                       prepared_body_sha256=hashlib.sha256(self.body).hexdigest())
            if self.captured:
                self.record["error_body"] = error_body(bytes(self.captured), self.record["response_headers"],
                                                         self.secrets, self.response_bytes > len(self.captured))
            self.record["environment"] = environment_snapshot(self.journal.proxies)
            destination = urlsplit(self.record["upstream"])
            self.record["configured_proxy_for_scheme"] = (safe_proxy(self.journal.proxies[destination.scheme])
                if self.record["proxy_policy"] != "direct_for_loopback" and destination.scheme in self.journal.proxies else None)
            self.journal._save(self.record)
        except Exception:
            self.journal.errors += 1


class _ObservedConnection:
    """Observe stdlib connections without replacing proxy, TLS or send behavior."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace = ACTIVE_TRACE.get()
        if self.trace:
            original = self._create_connection
            def connect_socket(*call_args, **call_kwargs):
                trace = self.trace
                trace.record["phase"] = "dns_or_tcp_connect"
                trace.record["socket_endpoint"] = {"host": self.host, "port": self.port,
                                                    "tunnel_target": self._tunnel_host}
                started = time.monotonic()
                try:
                    sock = original(*call_args, **call_kwargs)
                finally:
                    trace.record["timings_ms"]["dns_and_tcp"] = round((time.monotonic() - started) * 1000, 1)
                trace.record["peer_ip"] = sock.getpeername()[0]
                trace.record["phase"] = "proxy_connect" if self._tunnel_host else "tls" if isinstance(self, http.client.HTTPSConnection) else "request_write"
                return sock
            self._create_connection = connect_socket

    def _tunnel(self):
        if not self.trace:
            return super()._tunnel()
        self.trace.record["phase"] = "proxy_connect"
        started = time.monotonic()
        try:
            result = super()._tunnel()
            self.trace.record["phase"] = "tls" if isinstance(self, http.client.HTTPSConnection) else "request_write"
            return result
        finally:
            self.trace.record["timings_ms"]["proxy_connect"] = round((time.monotonic() - started) * 1000, 1)

    def connect(self):
        if not self.trace:
            return super().connect()
        started = time.monotonic()
        try:
            result = super().connect()
            if isinstance(self, http.client.HTTPSConnection):
                cipher = self.sock.cipher()
                self.trace.record["tls"] = {"version": self.sock.version(), "cipher": cipher[0] if cipher else None,
                                             "alpn": self.sock.selected_alpn_protocol()}
            self.trace.record["phase"] = "request_write"
            return result
        finally:
            timings = self.trace.record["timings_ms"]
            timings["connection_total"] = round((time.monotonic() - started) * 1000, 1)
            if isinstance(self, http.client.HTTPSConnection) and "dns_and_tcp" in timings:
                timings["tls_estimate"] = round(max(0, timings["connection_total"] - timings["dns_and_tcp"] - timings.get("proxy_connect", 0)), 1)

    def send(self, data):
        phase_before = self.trace.record["phase"] if self.trace else ""
        started = time.monotonic()
        try:
            result = super().send(data)
        except Exception:
            if self.trace:
                self.trace.record["flow"]["upload_interrupted"] = True
            raise
        if self.trace:
            flow = self.trace.record["flow"]
            field = "proxy_connect_header_bytes" if phase_before == "proxy_connect" else "uploaded_body_bytes" if data is self.trace.body else "uploaded_header_bytes"
            if isinstance(data, (bytes, bytearray, memoryview)):
                flow[field] = flow.get(field, 0) + len(data)
            timer = "body_upload" if data is self.trace.body else "headers_send_including_connection"
            timings = self.trace.record["timings_ms"]
            timings[timer] = round(timings.get(timer, 0) + (time.monotonic() - started) * 1000, 1)
            self.trace.record["phase"] = "proxy_connect" if phase_before == "proxy_connect" else "request_write"
        return result

    def getresponse(self):
        if not self.trace:
            return super().getresponse()
        self.trace.record["phase"] = "response_headers"
        started = time.monotonic()
        try:
            response = super().getresponse()
            self.trace.record["http_version"] = response.version
            return response
        finally:
            self.trace.record["timings_ms"]["response_header_wait"] = round((time.monotonic() - started) * 1000, 1)


class DiagnosticHTTPConnection(_ObservedConnection, http.client.HTTPConnection):
    pass


class DiagnosticHTTPSConnection(_ObservedConnection, http.client.HTTPSConnection):
    pass


class DiagnosticHTTPHandler(HTTPHandler):
    def do_open(self, http_class, req, **kwargs):
        return super().do_open(DiagnosticHTTPConnection, req, **kwargs)


class DiagnosticHTTPSHandler(HTTPSHandler):
    def do_open(self, http_class, req, **kwargs):
        return super().do_open(DiagnosticHTTPSConnection, req, **kwargs)
