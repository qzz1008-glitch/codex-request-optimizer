from __future__ import annotations

import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import sqlite3
import ssl
import sys
import threading
import time
from urllib import request as urllib_request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
import webbrowser

from . import VERSION
from .analyzer import (MAX_BODY, clean_text, describe_node, encode, inspect_request,
                       resolve, safe_path, search_body)
from .config_link import ConfigLink, validate_upstream
from .providers import ProviderManager, config_paths
from .entry_port import ConfiguredPortParser, port_status, save_port
from .updates import Updates
from .token_window import TokenWindows
from .session_summary import SessionSummaries
from .storage import Store
from .transport import prepare_request
from .installation import manage_installation
from .startup import configure_startup, startup_supported
from .capacity_retry import open_response
from .overflow import CheckpointStream, OverflowCompactor, OverflowFailure
from .image_config import ImageToolConfig, WORKER_HEADER
from .image_isolation import ImageIsolation, ImageIsolationFailure
from .image_tool import ImageToolRPC

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}
UI_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
            "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8")}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def outbound_proxies():
    proxies = urllib_request.getproxies()
    # NO_PROXY alone makes urllib skip the Windows system proxy settings.
    if not (set(proxies) - {"no"}):
        registry = getattr(urllib_request, "getproxies_registry", None)
        if registry is not None:
            proxies = {**registry(), **proxies}
    return proxies


class InspectorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir, config_path, upstream=None, *, worker_id="", public_port=None, worker_token=""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.link = ConfigLink(Path(config_path), self.data_dir / "codex-link.json")
        self.guard = threading.RLock()
        saved = json.loads(self.settings_path.read_text(encoding="utf8")) if self.settings_path.exists() else {}
        self.settings = {"relay_token": secrets.token_urlsafe(32),
                         "capture_enabled": True, "budget_bytes": 1_000_000,
                         "max_storage_mb": 512, "max_packets": 2000,
                         "request_compression": "zstd", "overflow_compaction_enabled": False,
                         "start_on_login": False, "capacity_retry_enabled": False,
                         "image_isolation_enabled": False,
                         "auto_detect_providers": True, "provider_routes": {}, **saved}
        self.settings.pop("ui_token", None)
        if upstream:
            self.settings["upstream"] = validate_upstream(upstream)
        elif not self.settings.get("upstream"):
            self.settings["upstream"] = self.link.profile()["upstream"]
        validate_upstream(self.settings["upstream"])
        self.capture_errors = 0
        self.active_requests = 0
        self.worker_id, self.worker_token = worker_id, worker_token
        self.worker_state = "standby" if worker_id else "active"
        self.maintenance_guard = threading.RLock()
        self.maintenance_enabled = not bool(worker_id)
        super().__init__(address, Handler)
        self.port = self.server_address[1]
        self.public_port = public_port or self.port
        self.provider_guard = threading.Lock()
        self.provider_fingerprint = None
        self.provider_sync_error = ""
        self.provider_sync_result = {}
        self.provider_next_scan = 0
        self.upgrade_draining = False
        self.token_windows = TokenWindows()
        self.session_summaries = SessionSummaries(self.token_windows)
        self.store = Store(self.data_dir, self.settings["max_storage_mb"] * 1024 * 1024,
                           self.settings["max_packets"], capture_owner="relay:" + worker_id if worker_id else "relay")
        if not worker_id:
            self.save_settings()
        ssl_context = ssl.create_default_context()
        self.opener = build_opener(ProxyHandler(outbound_proxies()), HTTPSHandler(context=ssl_context), NoRedirect())
        self.local_opener = build_opener(ProxyHandler({}), NoRedirect())
        self.overflow = OverflowCompactor(self.data_dir)
        self.image_config = ImageToolConfig(self.link, self.data_dir,
            lambda: f"http://127.0.0.1:{self.public_port}", lambda: self.endpoint)
        self.image_isolation = ImageIsolation(self.data_dir)
        self.image_tool = ImageToolRPC(self.image_config)
        self.updates = Updates(self.data_dir, self.link.config, self.public_port)
        self.analysis_errors = 0
        self.maintenance_errors = 0
        self.installation_guard = threading.Lock()
        self.startup_state_known = True
        if not worker_id and startup_supported(self.data_dir):
            # Reflect the existing login task without changing it during upgrade.
            try:
                installed = manage_installation("Status", self.data_dir, self.link.config, self.port)
                self.save_settings({**self.settings, "start_on_login": bool(installed.get("login_start"))})
            except Exception:
                # An optional Windows setting must not prevent the relay from starting.
                self.startup_state_known = False
        self.analysis_stop = threading.Event()
        self.analysis_thread = threading.Thread(target=self._classify_pending, daemon=True)
        self.analysis_thread.start()

    def _classify_pending(self):
        # Update classifications of stored records without delaying list requests.
        while not self.analysis_stop.is_set():
            with self.maintenance_guard:
                processed = self._maintenance_step() if self.maintenance_enabled else 0
            self.analysis_stop.wait(0.02 if processed else 2)

    def _maintenance_step(self):
        try:
            processed = self.store.reclassify_pending(limit=8)
        except Exception:
            self.analysis_errors += 1
            processed = 0
        try:
            if self.settings["auto_detect_providers"] and self.provider_fingerprint is not None:
                fingerprint = self._provider_fingerprint()
                if fingerprint != self.provider_fingerprint or time.monotonic() >= self.provider_next_scan:
                    self.sync_providers()
            self.overflow.maintenance()
            self.updates.poll()
            self.store.reclaimer.run(self.overflow.busy)
        except Exception:
            self.maintenance_errors += 1
        return processed

    def worker_control(self, data):
        action = data.get("action")
        with self.maintenance_guard:
            if action == "pause":
                self.maintenance_enabled = False
                self.worker_state = "draining"
            elif action == "activate":
                with self.guard:
                    if self.settings_path.exists():
                        saved = json.loads(self.settings_path.read_text(encoding="utf8"))
                        self.settings = {**self.settings, **saved}
                    if "public_port" in data:
                        from .entry_port import validate_port
                        self.public_port = validate_port(data["public_port"])
                    self.save_settings()
                    self.store.max_bytes = self.settings["max_storage_mb"] * 1024 * 1024
                    self.store.max_count = self.settings["max_packets"]
                if self.settings["auto_detect_providers"]:
                    self.sync_providers()
                self.worker_state = "active"
                self.maintenance_enabled = bool(data.get("maintenance", True))
            elif action == "maintenance":
                self.maintenance_enabled = self.worker_state == "active" and bool(data.get("enabled"))
            elif action == "recover":
                for owner in data.get("owners", []):
                    if not isinstance(owner, str) or not re.fullmatch(r"relay(?::[0-9a-f]{32})?", owner):
                        raise ValueError("invalid capture owner")
                    self.store.recover_owner(owner)
            elif action == "installation_refresh":
                if startup_supported(self.data_dir):
                    installed = manage_installation("Status", self.data_dir, self.link.config, self.public_port)
                    manage_installation("Install", self.data_dir, self.link.config, self.public_port)
                    if installed.get("login_start"):
                        configure_startup(True, self.data_dir, self.link.config, self.public_port)
                    self.save_settings({**self.settings, "start_on_login": bool(installed.get("login_start"))})
                    self.startup_state_known = True
            else:
                raise ValueError("invalid worker action")
        return {"worker_id": self.worker_id, "state": self.worker_state}

    def _provider_fingerprint(self):
        paths = config_paths(self.link.config)
        paths.append(self.link.config.parent / "auth.json")
        return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                     for path in paths if path.exists())

    def sync_providers(self):
        with self.provider_guard:
            if not self.settings["auto_detect_providers"]:
                return {"enabled": False}
            manager = ProviderManager(self.link, self.endpoint, self.settings["upstream"])
            def publish(routes):
                with self.guard:
                    self.save_settings({**self.settings, "provider_routes": routes})
            try:
                result = manager.sync(self.settings["provider_routes"], publish)
            except Exception as exc:
                self.provider_sync_error = type(exc).__name__
                self.provider_fingerprint = self._provider_fingerprint()
                self.provider_next_scan = time.monotonic() + 30
                raise
            self.provider_fingerprint = self._provider_fingerprint()
            self.provider_sync_error = ""
            self.provider_sync_result = result
            self.provider_next_scan = time.monotonic() + 30
            return result

    def server_close(self):
        # TCPServer also calls this when binding fails, before worker setup.
        stop = getattr(self, "analysis_stop", None)
        if stop is not None:
            stop.set()
        worker = getattr(self, "analysis_thread", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)
        super().server_close()

    def save_settings(self, candidate=None):
        staging = self.settings_path.with_suffix(".new")
        with self.guard:
            value = self.settings if candidate is None else candidate
            staging.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
            staging.replace(self.settings_path)
            self.settings = value

    @property
    def ui_url(self):
        return f"http://127.0.0.1:{self.port}/"

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.public_port}/relay/{self.settings['relay_token']}"

    def status(self):
        try:
            profile = self.link.profile()
        except Exception as exc:
            profile = {"active": False, "config_error": type(exc).__name__}
        storage = self.store.stats()
        return {"version": VERSION, "ui_version": VERSION, "capture_enabled": self.settings["capture_enabled"],
                "request_compression": self.settings["request_compression"],
                "overflow_compaction_enabled": self.settings["overflow_compaction_enabled"],
                "start_on_login": self.settings["start_on_login"],
                "start_on_login_supported": startup_supported(self.data_dir),
                "startup_state_known": self.startup_state_known,
                "capacity_retry_enabled": self.settings["capacity_retry_enabled"],
                "capacity_retry_supported": True,
                "image_isolation_enabled": self.settings["image_isolation_enabled"],
                "image_isolation_supported": True, "image_tool": self.image_config.status(),
                "budget_bytes": self.settings["budget_bytes"], "upstream": self.settings["upstream"],
                "max_storage_mb": self.settings["max_storage_mb"], "max_packets": self.settings["max_packets"],
                "capture_errors": self.capture_errors, "analysis_errors": self.analysis_errors,
                "maintenance_errors": self.maintenance_errors, "overflow_cache": self.overflow.cache_status(),
                "active_requests": self.active_requests,
                "relay_active_requests": self.active_requests,
                "auto_detect_providers": self.settings["auto_detect_providers"],
                "provider_routing_supported": True, "upgrade_drain_supported": True,
                "updates_supported": True,
                "entry_port_supported": True, "entry_port": port_status(self.data_dir, self.public_port),
                "worker_id": self.worker_id, "worker_state": self.worker_state,
                "public_port": self.public_port,
                "provider_count": self.provider_sync_result.get("provider_count", len({r["provider"] for r in self.settings["provider_routes"].values()})),
                "provider_sync_error": self.provider_sync_error,
                "provider_warnings": self.provider_sync_result.get("warnings", []),
                "desktop_tray": bool(getattr(self, "desktop_tray", False)),
                "data_dir": str(self.data_dir), "profile": profile, "storage": storage,
                "scope": "HTTP requests through configured providers; external overrides and WebSockets are not captured",
                "capture_point": "Codex app exit, before any existing downstream gateway"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexPacketInspector/" + VERSION

    def log_message(self, *_args):
        pass  # Do not log URLs containing capabilities, request bodies, or headers.

    def _valid_host(self):
        allowed = {f"{host}:{port}" for host in ("127.0.0.1", "localhost")
                   for port in (self.server.port, self.server.public_port)}
        return self.headers.get("Host", "") in allowed

    def _valid_origin(self):
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")
                                       for port in (self.server.port, self.server.public_port)}

    def _json(self, status, payload, headers=None):
        self._bytes(status, encode(payload), "application/json; charset=utf-8", headers)

    def _bytes(self, status, data, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _body(self, limit=MAX_BODY):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer and transfer != "chunked":
            raise ValueError("unsupported transfer encoding")
        if transfer:
            if self.headers.get("Content-Length"):
                raise ValueError("ambiguous request framing")
            chunks, size = [], 0
            while True:
                line = self.rfile.readline(128)
                length = int(line.split(b";", 1)[0].strip(), 16)
                if length < 0 or size + length > limit:
                    raise ValueError("request too large")
                if not length:
                    # Trailers are framing, not an additional request body.
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(8192)
                        trailer_bytes += len(trailer)
                        if trailer_bytes > 65536:
                            raise ValueError("trailers too large")
                        if trailer in {b"\r\n", b"\n", b""}:
                            break
                    return b"".join(chunks)
                chunk = self.rfile.read(length)
                if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                    raise ValueError("incomplete chunked body")
                chunks.append(chunk)
                size += length
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ValueError("request too large")
        result = self.rfile.read(length)
        if len(result) != length:
            raise ValueError("incomplete request body")
        return result

    def _handle(self):
        self.connection.settimeout(600)
        if not self._valid_host() or not self._valid_origin():
            self._json(403, {"error": "local_origin_required"})
            return
        path = urlsplit(self.path).path
        if path == "/__worker__/control":
            if (self.command != "POST" or not self.server.worker_id or
                    not secrets.compare_digest(self.headers.get("X-Inspector-Control", ""), self.server.worker_token)):
                self._json(403, {"error": "worker_control_required"})
                return
            try:
                data = json.loads(self._body(8192))
                if not isinstance(data, dict):
                    raise ValueError("invalid worker control")
                if data.get("action") == "stop":
                    with self.server.guard:
                        if self.server.active_requests:
                            self._json(409, {"error": "requests_in_flight"})
                            return
                        self.server.upgrade_draining = True
                    self._json(200, {"stopped": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._json(200, self.server.worker_control(data))
            except (OSError, ValueError, TypeError) as exc:
                self._json(400, {"error": type(exc).__name__})
            return
        relay_prefix = "/relay/" + self.server.settings["relay_token"]
        if path.startswith("/mcp/image/"):
            self._image_mcp(path)
            return
        if path.startswith(relay_prefix + "/"):
            self._relay(relay_prefix)
            return
        if path in UI_FILES and self.command == "GET":
            name, kind = UI_FILES[path]
            self._bytes(200, (Path(__file__).with_name("ui") / name).read_bytes(), kind)
            return
        if path == "/health" and self.command == "GET":
            self._json(200, {"ok": True, "app": "codex-packet-inspector", "version": VERSION,
                             "worker_id": self.server.worker_id})
            return
        try:
            self._api(path)
        except (KeyError, IndexError):
            self._json(404, {"error": "记录或字段不存在"})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": clean_text(str(exc))[:250]})
        except Exception as exc:
            self._json(500, {"error": type(exc).__name__})

    def _image_mcp(self, path):
        with self.server.guard:
            enabled = self.server.settings["image_isolation_enabled"]
            matches = secrets.compare_digest(path, self.server.image_config.mcp_path())
        if not enabled or not matches:
            self._json(404, {"error": "image_tool_unavailable"})
            return
        if self.command == "DELETE":
            with self.server.image_tool.guard:
                self.server.image_tool.sessions.pop(self.headers.get("Mcp-Session-Id", ""), None)
            self._bytes(204, b"", "application/json")
            return
        if self.command != "POST":
            self._json(405, {"error": "method_not_allowed"}, {"Allow": "POST, DELETE"})
            return
        try:
            message = json.loads(self._body(32768))
            result, headers = self.server.image_tool.dispatch(message, self.headers.get("Mcp-Session-Id"))
            if result is None:
                self._bytes(202, b"", "application/json", headers)
            else:
                self._json(200, result, headers)
        except KeyError:
            self._json(404, {"error": "image_tool_session_expired"})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": clean_text(str(exc))[:250]})

    def _api(self, path):
        query = parse_qs(urlsplit(self.path).query)
        one = lambda k, default="": query.get(k, [default])[0]
        if path == "/api/status" and self.command == "GET":
            self._json(200, self.server.status())
        elif path == "/api/updates" and self.command == "GET":
            self._json(200, self.server.updates.status())
        elif path == "/api/updates" and self.command == "POST":
            data = json.loads(self._body(8192))
            if not isinstance(data, dict):
                raise ValueError("invalid update request")
            self.server.updates.port = self.server.public_port
            result = (self.server.updates.configure(data) if data.get("action") == "configure"
                      else self.server.updates.start(data.get("action")))
            self._json(200, result)
        elif path == "/api/session-summaries" and self.command == "GET":
            try:
                self._json(200, self.server.session_summaries.read(self.server.data_dir))
            except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
                self._json(200, {"state": "unavailable", "sessions": []})
        elif path == "/api/installation" and self.command in {"GET", "POST"}:
            action = "Status" if self.command == "GET" else json.loads(self._body(1024)).get("action")
            if action not in {"Status", "Install", "Remove"}:
                raise ValueError("invalid installation action")
            with self.server.installation_guard:
                result = manage_installation(action, self.server.data_dir,
                                             self.server.link.config, self.server.port)
            self._json(200, result)
        elif path == "/api/context-settings" and self.command == "GET":
            with self.server.guard:
                result = self.server.link.context_settings()
            self._json(200, result)
        elif path == "/api/context-settings" and self.command == "POST":
            data = json.loads(self._body(8192))
            with self.server.guard:
                result = self.server.link.save_context_settings(data)
            self._json(200, result)
        elif path == "/api/packet-filters" and self.command == "GET":
            self._json(200, self.server.store.filter_options())
        elif path == "/api/packets" and self.command == "GET":
            before = float(one("before")) if one("before") else None
            self._json(200, self.server.store.list(limit=min(300, max(1, int(one("limit", "150")))),
                        thread=one("thread")[:200], query=one("q")[:200], before=before,
                        include_get=one("include_get") == "1", sort=one("sort", "created"),
                        direction=one("direction", "desc"), after=one("after")[:64] or None,
                        kind=one("kind"), status=one("status"), thread_unknown=one("thread_unknown") == "1"))
        elif path == "/api/entry-port" and self.command == "GET":
            self._json(200, port_status(self.server.data_dir, self.server.public_port))
        elif path == "/api/entry-port" and self.command == "POST":
            data = json.loads(self._body(8192))
            if not isinstance(data, dict):
                raise ValueError("invalid entry port settings")
            with self.server.guard:
                result = save_port(self.server.data_dir, self.server.public_port, data.get("port"))
            self._json(200, result)
        elif path == "/api/settings" and self.command == "POST":
            data = json.loads(self._body(8192))
            with self.server.provider_guard, self.server.guard:
                candidate = dict(self.server.settings)
                if "request_compression" in data:
                    if data["request_compression"] not in {"off", "zstd"}:
                        raise ValueError("invalid request compression mode")
                    candidate["request_compression"] = data["request_compression"]
                for key in ("capture_enabled", "overflow_compaction_enabled", "start_on_login", "capacity_retry_enabled", "image_isolation_enabled"):
                    if key in data:
                        if not isinstance(data[key], bool):
                            raise ValueError("invalid boolean setting: " + key)
                        candidate[key] = data[key]
                for key, low, high in (("budget_bytes", 1024, MAX_BODY), ("max_storage_mb", 16, 8192),
                                       ("max_packets", 10, 20000)):
                    if key in data:
                        value = data[key]
                        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                            raise ValueError("设置超出有效范围: " + key)
                        candidate[key] = value
                previous = self.server.settings
                with self.server.image_config.change(candidate["image_isolation_enabled"]):
                    self.server.save_settings(candidate)
                    try:
                        if "start_on_login" in data and (
                                candidate["start_on_login"] != previous["start_on_login"] or not self.server.startup_state_known):
                            with self.server.installation_guard:
                                configure_startup(candidate["start_on_login"], self.server.data_dir,
                                                      self.server.link.config, self.server.port)
                            self.server.startup_state_known = True
                    except Exception:
                        self.server.save_settings(previous)
                        raise
                self.server.store.max_bytes = candidate["max_storage_mb"] * 1024 * 1024
                self.server.store.max_count = candidate["max_packets"]
            self._json(200, self.server.status())
        elif path == "/api/link" and self.command == "POST":
            data = json.loads(self._body(8192))
            if data.get("action") == "restore":
                with self.server.provider_guard:
                    self.server.save_settings({**self.server.settings, "auto_detect_providers": False})
                    result = self.server.link.restore()
            elif data.get("action") == "activate":
                self.server.save_settings({**self.server.settings, "auto_detect_providers": True})
                result = self.server.sync_providers()
            else:
                raise ValueError("invalid link action")
            self._json(200, result)
        elif path == "/api/providers/sync" and self.command == "POST":
            self._body(8192)
            result = self.server.sync_providers() if self.server.settings["auto_detect_providers"] else {"enabled": False}
            self._json(200, result)
        elif path == "/api/upgrade/stop" and self.command == "POST":
            self._body(8192)
            with self.server.guard:
                if self.server.active_requests:
                    self._json(409, {"error": "requests_in_flight", "active_requests": self.server.active_requests})
                    return
                self.server.upgrade_draining = True
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif path == "/api/stop" and self.command == "POST":
            if self.server.link.profile()["active"]:
                raise ValueError("请先恢复原出口并重新加载 Codex，再退出采集器")
            if self.server.active_requests:
                raise ValueError("仍有请求正在转发，请稍后退出")
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif path.startswith("/api/packets/") and self.command == "GET":
            parts = path.split("/")
            packet_id = parts[3]
            action = parts[4] if len(parts) > 4 else ""
            if action == "diff":
                self._json(200, self.server.store.compare(packet_id))
                return
            meta, content = self.server.store.get(packet_id, content=bool(action) and action != "token-window")
            if action == "token-window":
                try:
                    following = self.server.store.list(limit=1, thread=meta.get("thread_id", ""),
                                                       sort="created", direction="asc", after=packet_id)
                    next_created = following[0].get("created") if following else None
                    self._json(200, self.server.token_windows.lookup_ready(meta, next_created))
                except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
                    self._json(200, {"state": "unavailable"})
            elif action == "node":
                if not content:
                    raise ValueError("此请求没有可解析的 JSON 正文")
                self._json(200, describe_node(content["body"], one("path"), content["source_sizes"],
                                             max(0, int(one("offset", "0"))), 100))
            elif action == "text":
                if not content:
                    raise ValueError("此请求没有可解析的 JSON 正文")
                node = resolve(content["body"], one("path"))
                offset = max(0, int(one("offset", "0")))
                text = node if isinstance(node, str) else json.dumps(node, ensure_ascii=False, indent=2)
                self._json(200, {"text": text[offset:offset + 32768], "offset": offset,
                                 "next_offset": offset + 32768 if len(text) > offset + 32768 else None,
                                 "total_chars": len(text)})
            elif action == "search":
                self._json(200, search_body(content["body"], one("q")[:200]) if content else [])
            elif action == "export":
                exported = {"metadata": meta, "view": content,
                            "notice": "Sanitized viewing copy, not raw replay input. Exact sizes were measured before redaction."}
                self._bytes(200, encode(exported), "application/json; charset=utf-8",
                            {"Content-Disposition": 'attachment; filename="codex-packet-' + packet_id + '.json"'})
            elif not action:
                self._json(200, meta)
            else:
                self._json(404, {"error": "unknown operation"})
        else:
            self._json(404, {"error": "not_found"})

    def _relay(self, prefix):
        with self.server.guard:
            if self.server.upgrade_draining:
                self.close_connection = True
                self._json(503, {"error": "service_upgrading"})
                return
            self.server.active_requests += 1
        try:
            with self.server.overflow.request_scope() as scope:
                self._relay_request(prefix, scope)
        finally:
            with self.server.guard:
                self.server.active_requests -= 1

    def _checkpoint(self, upstream, suffix, headers, body, encoding):
        # This is a separate, tool-free summary call. The original turn has not
        # been uploaded. Never follow redirects or retry either request.
        remove = {"content-encoding", "accept", "accept-encoding", "content-md5", "digest",
                  "x-request-id", "x-codex-turn-metadata", "x-codex-client-metadata"}
        compact_headers = {k: v for k, v in headers.items() if k.lower() not in remove}
        compact_headers.update({"Content-Encoding": encoding, "Accept": "text/event-stream", "Accept-Encoding": "identity"})
        local = urlsplit(upstream).hostname in {"localhost", "127.0.0.1", "::1"}
        opener = self.server.local_opener if local else self.server.opener
        try:
            response = opener.open(Request(upstream + suffix, data=body, headers=compact_headers, method="POST"), timeout=120)
        except HTTPError as exc:
            exc.close()
            raise OverflowFailure("overflow_compaction_upstream_error", "上游压缩请求返回 HTTP " + str(exc.code) + "；原请求未发送。", 502) from None
        parser = CheckpointStream()
        with response:
            if response.status != 200:
                raise OverflowFailure("overflow_compaction_upstream_error", "上游没有接受压缩请求；原请求未发送。", 502)
            reader = getattr(response, "read1", response.read)
            while parser.completed is None:
                chunk = reader(65536)
                if not chunk:
                    break
                parser.feed(chunk)
        return parser.finish()

    def _relay_request(self, prefix, scope):
        if self.command not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            self._json(405, {"error": "method_not_supported"})
            return
        try:
            body = self._body()
        except (ValueError, OSError):
            self._json(400, {"error": "invalid_request_body"})
            return
        suffix = self.path[len(prefix):]
        if not suffix.startswith("/") or suffix.startswith("//"):
            self._json(400, {"error": "invalid_request_path"})
            return
        with self.server.guard:
            upstream = self.server.settings["upstream"]
            if suffix.startswith("/p/"):
                parts = suffix.split("/", 3)
                route = self.server.settings["provider_routes"].get(parts[2])
                if len(parts) != 4 or not route:
                    self._json(404, {"error": "unknown_provider_route"})
                    return
                upstream = route["upstream"]
                suffix = "/" + parts[3]
                if suffix.startswith("//"):
                    self._json(400, {"error": "invalid_request_path"})
                    return
        started = time.monotonic()
        packet_id = None
        if self.server.settings["capture_enabled"]:
            try:
                meta, content = inspect_request(body, dict(self.headers.items()), suffix)
                meta.update(method=self.command, path=safe_path(suffix), source="live",
                            capture_point="app_exit_before_downstream")
                packet_id = self.server.store.add(meta, content)
            except Exception:
                self.server.capture_errors += 1
        def update(**fields):
            if packet_id:
                try:
                    self.server.store.update(packet_id, **fields)
                except Exception:
                    self.server.capture_errors += 1
        connection_tokens = {s.strip().lower() for s in self.headers.get("Connection", "").split(",")}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP | connection_tokens | {"host", "content-length", "x-codex-inspector-packet-id", WORKER_HEADER.lower()}}
        # urllib preserves content bytes, does not decompress responses, and
        # uses the OS/environment proxy. Redirects and inference retries are disabled.
        with self.server.guard:
            compression = self.server.settings["request_compression"]
            overflow_enabled = self.server.settings["overflow_compaction_enabled"]
            budget = self.server.settings["budget_bytes"]
            image_enabled = self.server.settings["image_isolation_enabled"]
            image_worker = self.server.image_config.is_worker(self.headers.get(WORKER_HEADER, ""))
        try:
            prepared = None
            if image_enabled and not image_worker and self.command == "POST" and urlsplit(suffix).path.rstrip("/").endswith(("/responses", "/responses/compact")):
                prepared = self.server.image_isolation.prepare(body, headers, suffix, compression)
            if prepared is not None:
                outbound_body, outbound_encoding, image_meta = prepared
                update(**image_meta)
                headers = {k: v for k, v in headers.items() if k.lower() not in {"content-md5", "digest"}}
            else:
                outbound_body, outbound_encoding = prepare_request(body, headers, self.command, suffix, compression)
            if image_worker:
                update(image_isolation_worker=True)
            if overflow_enabled and not image_worker and self.command == "POST" and urlsplit(suffix).path.rstrip("/").endswith("/responses"):
                outbound_body, outbound_encoding, overflow_meta = self.server.overflow.prepare(
                    outbound_body, outbound_encoding, body, headers, suffix, upstream, compression, budget,
                    lambda compact_body, encoding: self._checkpoint(upstream, suffix, headers, compact_body, encoding),
                    request_scope=scope)
                update(**overflow_meta)
        except ImageIsolationFailure as exc:
            update(state="relay_error", relay_error=exc.code, status=exc.status)
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except ImportError:
            update(state="relay_error", relay_error="zstd_unavailable", status=503)
            self._json(503, {"error": {"code": "zstd_unavailable", "message": "缺少 zstd 依赖，请重新安装完整程序或关闭传输压缩。"}})
            return
        except OverflowFailure as exc:
            update(state="relay_error", relay_error=exc.code, status=exc.status,
                   overflow_original_wire_bytes=len(outbound_body),
                   duration_ms=round((time.monotonic() - started) * 1000, 1))
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except Exception as exc:
            update(state="relay_error", relay_error="overflow_" + type(exc).__name__, status=503)
            self._json(503, {"error": {"code": "overflow_preparation_failed", "message": "本地上传包准备失败；原请求未发送。"}})
            return
        if outbound_body is not body:
            headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
            headers["Content-Encoding"] = outbound_encoding
        update(relay_wire_bytes=len(outbound_body), relay_content_encoding=outbound_encoding,
               compression_applied=outbound_body is not body)
        local_upstream = urlsplit(upstream).hostname in {"localhost", "127.0.0.1", "::1"}
        capacity_retry = (self.server.settings["capacity_retry_enabled"] and self.command == "POST"
                          and urlsplit(suffix).path.rstrip("/").endswith("/responses"))
        if capacity_retry:
            headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
        request = Request(upstream + suffix,
                          data=outbound_body if body or self.command in {"POST", "PUT", "PATCH"} else None,
                          headers=headers, method=self.command)
        sent_headers = False
        received = 0
        try:
            try:
                # A separate local upstream can transform the body again, so its
                # final upload is unknown. Direct uploads use these exact bytes.
                if not local_upstream:
                    update(outbound_wire_bytes=len(outbound_body),
                           outbound_content_encoding=outbound_encoding)
                opener = self.server.local_opener if local_upstream else self.server.opener
                response = open_response(
                    opener, request, enabled=capacity_retry,
                    on_retry=lambda attempt, delay: update(capacity_retry_count=attempt,
                                                          capacity_retry_delay_ms=round(delay * 1000),
                                                          capacity_retry_reason="server_overloaded"),
                    cancelled=self._client_disconnected,
                    still_enabled=lambda: self.server.settings["capacity_retry_enabled"])
            except HTTPError as exc:
                response = exc
            with response:
                status = response.status
                update(status=status, state="streaming", response_type=response.headers.get("Content-Type", ""),
                       headers_ms=round((time.monotonic() - started) * 1000, 1))
                self.send_response(status, response.reason)
                response_tokens = {s.strip().lower() for s in response.headers.get("Connection", "").split(",")}
                for key, value in response.headers.items():
                    if key.lower() not in HOP | response_tokens | {"content-length"}:
                        self.send_header(key, value)
                if response.headers.get("Content-Length") is not None:
                    self.send_header("Content-Length", response.headers["Content-Length"])
                self.send_header("Connection", "close")
                self.end_headers()
                sent_headers = True
                if self.command != "HEAD":
                    reader = getattr(response, "read1", response.read)
                    while True:
                        chunk = reader(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        received += len(chunk)
                update(state="complete", response_bytes=received,
                       duration_ms=round((time.monotonic() - started) * 1000, 1))
        except (BrokenPipeError, ConnectionResetError):
            update(state="client_disconnected", response_bytes=received,
                   duration_ms=round((time.monotonic() - started) * 1000, 1))
        except Exception as exc:
            update(state="relay_error", relay_error=type(exc).__name__, response_bytes=received,
                   duration_ms=round((time.monotonic() - started) * 1000, 1))
            if not sent_headers:
                self._json(502, {"error": {"code": "inspector_upstream_unreachable",
                            "message": "上游连接失败: " + type(exc).__name__}})
        finally:
            self.close_connection = True

    def _client_disconnected(self):
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


def default_data_dir():
    # A per-user directory outside AppData also works when Windows starts the
    # observer outside an MSIX app's virtualized filesystem.
    return Path.home() / ".codex-packet-inspector"


def argument_parser():
    parser = ConfiguredPortParser(description="Local Codex packet inspector")
    parser.add_argument("--port", type=int, default=None, help="override the configured entry port")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    parser.add_argument("--upstream")
    parser.add_argument("--worker-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--public-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--open", action="store_true")
    return parser


def main(argv=None, on_started=None):
    args = argument_parser().parse_args(argv)
    worker_token = ""
    if args.worker_id:
        if not re.fullmatch(r"[0-9a-f]{32}", args.worker_id):
            raise ValueError("invalid worker identity")
        control = json.loads((args.data_dir / "router-workers" / (args.worker_id + ".json")).read_text(encoding="utf8"))
        worker_token = control["token"]
    try:
        server = InspectorServer(("127.0.0.1", args.port), args.data_dir, args.config, args.upstream,
                                 worker_id=args.worker_id, public_port=args.public_port, worker_token=worker_token)
    except OSError as exc:
        if args.worker_id:
            raise RuntimeError("worker port is occupied") from None
        # Reopening the app reuses its own local service.
        settings_file = args.data_dir / "settings.json"
        if settings_file.exists():
            from urllib.request import urlopen
            try:
                url = f"http://127.0.0.1:{args.port}"
                health = json.load(urlopen(url + "/health", timeout=2))
                req = Request(url + "/api/status")
                status = json.load(urlopen(req, timeout=2))
                if health.get("app") == "codex-packet-inspector":
                    if (Path(status["data_dir"]).resolve() != args.data_dir.resolve()
                            or Path(status["profile"]["config_path"]).resolve() != args.config.resolve()):
                        raise ValueError("existing service uses a different profile")
                    if status.get("provider_routing_supported") and status.get("auto_detect_providers"):
                        sync = Request(url + "/api/providers/sync", data=b"{}", headers={"Content-Type": "application/json"})
                        json.load(urlopen(sync, timeout=10))
                    if args.open:
                        webbrowser.open(url + "/")
                    return 0
            except Exception:
                pass
        raise RuntimeError("本地端口已被占用或程序无法启动: " + type(exc).__name__) from None
    runtime = {"pid": os.getpid(), "port": server.port, "version": VERSION}
    runtime_path = (args.data_dir / "router-workers" / (args.worker_id + ".runtime.json")
                    if args.worker_id else args.data_dir / "runtime.json")
    runtime_path.write_text(json.dumps(runtime), encoding="utf8")
    try:
        if not args.worker_id and server.settings["auto_detect_providers"]:
            try:
                server.sync_providers()
            except (OSError, ValueError, TypeError):
                pass  # Keep already-published routes serving; expose the error in status.
        if on_started:
            on_started(server)
        if args.open:
            threading.Timer(0.4, lambda: webbrowser.open(server.ui_url)).start()
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
