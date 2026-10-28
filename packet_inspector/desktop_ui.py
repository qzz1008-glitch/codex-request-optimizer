"""Serve this build's UI while leaving the existing forwarding service alone."""
from __future__ import annotations

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlencode, urlsplit

from . import VERSION
from .build_info import build_info
from .token_window import TokenWindows, is_token_request
from .response_usage import packet_usage
from .session_summary import SessionSummaries
from .entry_port import port_status, save_port
from .updates import Updates
from .http_io import discard_small_body
from .exit_flow import ExitError, ExitRequests, perform_exit

UI_DIR = Path(__file__).with_name("ui")
UI_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
            "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8")}
POST_PATHS = {"/api/settings", "/api/context-settings", "/api/link"}
GET_PATHS = {"/api/status", "/api/tray-status", "/api/context-settings", "/api/packet-filters", "/api/packets"}


def window_geometry(ui_dir=None):
    """Read the native window dimensions from the shared UI stylesheet."""
    css = (Path(ui_dir or UI_DIR) / "style.css").read_text(encoding="utf8")
    values = {}
    for key in ("width", "height", "min-width", "min-height"):
        match = re.search(r"--desktop-" + key + r"\s*:\s*(\d+)px", css)
        if not match:
            raise ValueError("missing desktop window dimension: " + key)
        values[key] = int(match[1])
    if not (800 <= values["min-width"] <= values["width"] <= 1920
            and 500 <= values["min-height"] <= values["height"] <= 1200):
        raise ValueError("invalid desktop window dimensions")
    return {"width": values["width"], "height": values["height"],
            "min_size": (values["min-width"], values["min-height"])}


def ui_build_id(ui_dir=None):
    digest = hashlib.sha256(VERSION.encode())
    for name, _kind in UI_FILES.values():
        digest.update((Path(ui_dir or UI_DIR) / name).read_bytes())
    return digest.hexdigest()[:20]


class DesktopUI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, relay_port, *, live_reload=False, ui_dir=None, exit_data_dir=None):
        if not 1 <= relay_port <= 65535:
            raise ValueError("invalid forwarding service port")
        self.relay_port = relay_port
        self.ui_dir = Path(ui_dir or UI_DIR)
        self.live_reload = bool(live_reload)
        self.token_windows = TokenWindows()
        self.session_summaries = SessionSummaries(self.token_windows)
        self.updates = None
        self.updates_guard = threading.Lock()
        self.exit_requests = ExitRequests(exit_data_dir) if exit_data_dir is not None else None
        self.window_opened = 0
        self.thread = None
        super().__init__(("127.0.0.1", 0), UIHandler)
        self.port = self.server_address[1]

    @property
    def ui_url(self):
        return f"http://127.0.0.1:{self.port}/"

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.thread:
            self.shutdown()
            self.thread.join(timeout=2)
        self.server_close()


class UIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _headers(self, status, kind, length=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.close_connection = True

    def _bytes(self, status, data, kind):
        self._headers(status, kind, len(data))
        self.wfile.write(data)

    def _json(self, status, value):
        self._bytes(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _reject_json(self, status, value):
        discard_small_body(self)
        self._json(status, value)

    def _relay_json(self, path, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.relay_port, timeout=5)
        try:
            body = None if data is None else json.dumps(data).encode()
            headers = {"Origin": f"http://127.0.0.1:{self.server.relay_port}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request("GET" if data is None else "POST", path, body, headers)
            response = connection.getresponse()
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("metadata response too large")
            return response.status, json.loads(data)
        finally:
            connection.close()

    def _exit(self):
        requests = self.server.exit_requests
        if requests is None:
            self._json(404, {"error": "ui_route_not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 1024:
                raise ValueError()
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("action") not in {"restore", "direct", "disconnected", "cancel"}:
                raise ValueError()
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid_ui_request_body"})
            return
        try:
            requests.begin(data.get("request_id"))
        except ValueError as exc:
            self._json(409, {"error": str(exc)})
            return
        completed = False
        try:
            result = ({"cancelled": True} if data["action"] == "cancel"
                      else perform_exit(self._relay_json, data["action"]))
            completed = True
            self._json(200, result)
        except ExitError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            self._json(502, {"error": "退出未完成，请检查中转服务后重试。"})
        finally:
            requests.finish(completed=completed)

    def _token_window(self, packet_id):
        try:
            status, packet = self._relay_json("/api/packets/" + packet_id)
            if status != 200:
                self._json(status, {"state": "unavailable"})
                return
            if not is_token_request(packet):
                self._json(200, {"state": "unavailable"})
                return
            if packet_usage(packet) is not None:
                self._json(200, self.server.token_windows.lookup_ready(packet))
                return
            query = urlencode({"thread": packet.get("thread_id", ""), "limit": 32,
                               "sort": "created", "direction": "asc", "after": packet_id})
            status, following = self._relay_json("/api/packets?" + query)
            if status != 200 or not isinstance(following, list):
                self._json(200, {"state": "unavailable"})
                return
            following_model = next((row for row in following if is_token_request(row, include_compaction=True)), None)
            if following_model is None and len(following) >= 32:
                self._json(200, {"state": "unavailable"})
                return
            next_created = following_model.get("created") if following_model else None
            self._json(200, self.server.token_windows.lookup_ready(packet, next_created))
        except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError):
            self._json(200, {"state": "unavailable"})

    def _session_summaries(self):
        try:
            status, data = self._relay_json("/api/status")
            if status != 200 or not isinstance(data.get("data_dir"), str):
                self._json(200, {"state": "unavailable", "sessions": []})
                return
            self._json(200, self.server.session_summaries.read(data["data_dir"]))
        except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError, sqlite3.Error):
            self._json(200, {"state": "unavailable", "sessions": []})

    def _entry_port(self):
        try:
            value = None
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 8192:
                    raise ValueError("invalid_ui_request_body")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("invalid_ui_request_body")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("invalid_entry_port_settings")
                value = data.get("port")
            code, status = self._relay_json("/api/status")
            if code != 200 or not isinstance(status.get("data_dir"), str):
                self._json(503, {"error": "无法读取中转配置目录。"})
                return
            # Entry configuration is independent of the running relay settings.
            # It never changes the live relay, provider URLs or running requests.
            result = (save_port(status["data_dir"], self.server.relay_port, value)
                      if self.command == "POST" else port_status(status["data_dir"], self.server.relay_port))
            self._json(200, result)
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except (OSError, http.client.HTTPException):
            self._json(503, {"error": "入口端口配置暂不可用。"})

    def _updates(self):
        try:
            data = None
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 8192:
                    raise ValueError("invalid_ui_request_body")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("invalid_ui_request_body")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("invalid_update_request")
            code, status = self._relay_json("/api/status")
            if code != 200:
                raise ValueError("无法读取当前中转版本。")
            with self.server.updates_guard:
                if self.server.updates is None:
                    self.server.updates = Updates(status["data_dir"], status["profile"]["config_path"],
                                                  self.server.relay_port, status["version"])
                self.server.updates.current_version = status["version"]
                updates = self.server.updates
            if data is None:
                if not status.get("updates_supported"):
                    updates.poll()
                result = updates.status()
            elif data.get("action") == "configure":
                result = updates.configure(data)
                if result["automatic"] and not status.get("updates_supported"):
                    updates.start("check")
            else:
                result = updates.start(data.get("action"))
            self._json(200, result)
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except (OSError, KeyError, http.client.HTTPException):
            self._json(503, {"error": "版本更新功能暂不可用。"})

    def _handle(self):
        self.connection.settimeout(30)
        allowed_hosts = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in allowed_hosts or (origin and origin not in {"http://" + host for host in allowed_hosts}):
            self._reject_json(403, {"error": "local_origin_required"})
            return
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment:
            self._reject_json(400, {"error": "invalid_ui_path"})
            return
        path = target.path
        if path == "/api/exit" and self.command == "POST":
            self._exit()
            return
        if path == "/api/updates" and self.command in {"GET", "POST"}:
            self._updates()
            return
        if path == "/api/entry-port" and self.command in {"GET", "POST"}:
            self._entry_port()
            return
        if self.command == "GET" and path == "/api/session-summaries":
            self._session_summaries()
            return
        token_route = re.fullmatch(r"/api/packets/([0-9a-f]{32})/token-window", path)
        if self.command == "GET" and token_route:
            self._token_window(token_route[1])
            return
        if self.command == "GET" and self.server.live_reload and path == "/__dev__/state":
            try:
                self._json(200, {"app": "codex-packet-inspector-desktop-ui", "build": ui_build_id(self.server.ui_dir),
                                 "ui_version": VERSION, "relay_port": self.server.relay_port})
            except OSError:
                self._json(503, {"error": "ui_files_updating"})
            return
        if self.command == "GET" and self.server.live_reload and path == "/__dev__/reload.js":
            try:
                initial = json.dumps(ui_build_id(self.server.ui_dir))
            except OSError:
                self._json(503, {"error": "ui_files_updating"})
                return
            script = """\"use strict\";
(() => {
  const initial = INITIAL;
  window.addEventListener('keydown', event => {
    if (event.key === 'F5' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'r')) {
      event.preventDefault();
      if (!event.repeat) location.reload();
    }
  }, true);
  let checking = false, pending = null;
  setInterval(async () => {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch('/__dev__/state', {cache: 'no-store'});
      if (!response.ok) return;
      const {build} = await response.json();
      if (build !== initial && build === pending) location.reload();
      pending = build;
    } catch (_) {} finally { checking = false; }
  }, 1000);
})();
""".replace("INITIAL", initial)
            self._bytes(200, script.encode(), "text/javascript; charset=utf-8")
            return
        if self.command == "GET" and path in UI_FILES:
            name, kind = UI_FILES[path]
            try:
                data = (self.server.ui_dir / name).read_bytes()
                reload_page = self.server.live_reload
            except OSError:
                self._json(503, {"error": "ui_files_updating"})
                return
            if path == "/" and reload_page:
                data = data.replace(b"</head>", b'<script src="/__dev__/reload.js" defer></script></head>', 1)
            self._bytes(200, data, kind)
            return
        allowed = (self.command == "GET" and (path in GET_PATHS or path.startswith("/api/packets/"))) or (self.command == "POST" and path in POST_PATHS)
        if not allowed:
            self._reject_json(404, {"error": "ui_route_not_found"})
            return
        body = None
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 <= length <= 8192:
                    raise ValueError()
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError()
            except ValueError:
                self._json(400, {"error": "invalid_ui_request_body"})
                return
        connection = http.client.HTTPConnection("127.0.0.1", self.server.relay_port, timeout=30)
        sent = False
        try:
            headers = {"Origin": f"http://127.0.0.1:{self.server.relay_port}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(self.command, self.path, body, headers)
            response = connection.getresponse()
            kind = response.getheader("Content-Type", "application/json; charset=utf-8")
            if path == "/api/status" and response.status == 200:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError("status response too large")
                status = json.loads(data)
                status["ui_version"] = VERSION
                status["ui_build"] = build_info()
                status["ui_live_reload"] = self.server.live_reload
                status["ui_desktop"] = self.server.exit_requests is not None
                status["ui_window_opened"] = self.server.window_opened
                if self.server.exit_requests is not None:
                    status["ui_exit_request"] = self.server.exit_requests.current()
                if isinstance(status.get("data_dir"), str):
                    status["entry_port_supported"] = True
                    status["entry_port"] = port_status(status["data_dir"], self.server.relay_port)
                sent = True
                self._json(200, status)
            else:
                length = response.getheader("Content-Length")
                self._headers(response.status, kind, int(length) if length is not None else None)
                sent = True
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            if not sent:
                self._json(502, {"error": "forwarding_service_unavailable"})
        finally:
            connection.close()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle
