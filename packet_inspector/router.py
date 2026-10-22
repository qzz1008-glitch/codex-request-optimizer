"""Protocol 1 entry router. Standard library only; independent of app releases."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .http_io import discard_small_body

ROUTER_VERSION = "1.0.0"
PROTOCOL = 1
MAX_BODY = 128 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("w", encoding="utf8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def local_json(port, path, data=None, token="", *, timeout=10):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = None if data is None else json.dumps(data).encode()
        headers = {"Content-Type": "application/json", "X-Inspector-Control": token}
        connection.request("GET" if data is None else "POST", path, body, headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("control response too large")
        result = json.loads(raw)
        if response.status != 200:
            raise ValueError("control HTTP " + str(response.status))
        return result
    finally:
        connection.close()


def worker_info(data_dir, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("invalid worker identity")
    data = json.loads((Path(data_dir) / "router-workers" / (identity + ".json")).read_text(encoding="utf8"))
    if data.get("id") != identity or isinstance(data.get("port"), bool) or not isinstance(data.get("port"), int) or not 1024 <= data["port"] <= 65535:
        raise ValueError("invalid worker endpoint")
    return data


def control_worker(data_dir, identity, action, **values):
    worker = worker_info(data_dir, identity)
    return local_json(worker["port"], "/__worker__/control", {"action": action, **values}, worker["token"], timeout=30)


def healthy_worker(data_dir, identity):
    worker = worker_info(data_dir, identity)
    health = local_json(worker["port"], "/health", timeout=2)
    if health.get("worker_id") != identity or health.get("version") != worker["version"]:
        raise ValueError("worker identity mismatch")
    return worker


def start_worker(data_dir, config, identity, public_port):
    worker = worker_info(data_dir, identity)
    executable = Path(worker["executable"]).resolve()
    versions = (Path(data_dir) / "bin" / "versions").resolve()
    if not executable.is_relative_to(versions) or not executable.is_file():
        raise ValueError("worker executable is outside managed versions")
    return subprocess.Popen([str(executable), "--background", "--worker-id", identity,
                             "--public-port", str(public_port), "--port", str(worker["port"]),
                             "--data-dir", str(Path(data_dir).resolve()), "--config", str(Path(config).resolve())],
                            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not evidence of exit.
        try:
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, data_dir, config):
        self.data_dir, self.config = Path(data_dir), Path(config)
        self.state_path = self.data_dir / "router-state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf8"))
        if self.state.get("protocol") != PROTOCOL or Path(self.state["config"]).resolve() != self.config.resolve():
            raise ValueError("router profile or protocol mismatch")
        self.guard, self.change_guard = threading.RLock(), threading.RLock()
        self.counts, self.relay_counts = {}, {}
        self.stopping = False
        self.exit_requested = False
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.retiring = set()
        self.restart_after = 0
        super().__init__(address, RouterHandler)
        self.port = self.server_address[1]

    @property
    def ui_url(self):
        return f"http://127.0.0.1:{self.port}/"

    def status(self):
        with self.guard:
            return {"protocol": PROTOCOL, "router_version": ROUTER_VERSION, "port": self.port,
                    "active": self.state["active"], "draining": list(self.state.get("draining", [])),
                    "requests": dict(self.counts), "relay_requests": sum(self.relay_counts.values())}

    def acquire(self, relay):
        with self.guard:
            if self.stopping:
                raise ValueError("router stopping")
            identity = self.state["active"]
            worker = worker_info(self.data_dir, identity)
            if worker["port"] == self.port:
                raise ValueError("router loop")
            self.counts[identity] = self.counts.get(identity, 0) + 1
            if relay:
                self.relay_counts[identity] = self.relay_counts.get(identity, 0) + 1
            return identity, worker

    def release(self, identity, relay):
        with self.guard:
            self.counts[identity] -= 1
            if relay:
                self.relay_counts[identity] -= 1

    def switch(self, identity, expected=None):
        with self.change_guard, self.guard:
            previous = self.state["active"]
            if expected is not None and expected != previous:
                raise ValueError("active worker changed; retry the upgrade")
            if previous == identity:
                return self.status()
            candidate = healthy_worker(self.data_dir, identity)
            if candidate["port"] == self.port:
                raise ValueError("router loop")
            previous_alive = False
            try:
                healthy_worker(self.data_dir, previous)
                control_worker(self.data_dir, previous, "pause")
                previous_alive = True
            except (OSError, ValueError, http.client.HTTPException):
                if self.counts.get(previous, 0):
                    raise ValueError("previous backend is unreachable with requests in flight") from None
            committed = False
            try:
                control_worker(self.data_dir, identity, "activate", public_port=self.port, maintenance=False)
                status = local_json(candidate["port"], "/api/status")
                if status.get("provider_sync_error") or status.get("worker_id") != identity:
                    raise ValueError("candidate validation failed")
                draining = [entry for entry in self.state.get("draining", []) if entry["id"] != identity]
                draining.append({"id": previous, "not_before": time.time() + 30})
                updated = {**self.state, "active": identity, "draining": draining, "port": self.port}
                atomic_json(self.state_path, updated)
                self.state = updated
                committed = True
                return self.status()
            finally:
                if not committed:
                    try:
                        control_worker(self.data_dir, identity, "pause")
                    finally:
                        if previous_alive:
                            control_worker(self.data_dir, previous, "activate", public_port=self.port, maintenance=True)

    def _retire(self, identity):
        try:
            with self.change_guard, self.guard:
                if self.state["active"] == identity or self.counts.get(identity, 0):
                    return
                try:
                    control_worker(self.data_dir, identity, "stop")
                except (ConnectionError, OSError, http.client.HTTPException):
                    pass  # A backend that already exited still needs its owner recovered.
            # Never kill a backend or close an active response to meet a deadline.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    healthy_worker(self.data_dir, identity)
                except (OSError, ValueError, http.client.HTTPException):
                    break
                self.monitor_stop.wait(.1)
            else:
                return
            with self.change_guard, self.guard:
                if self.state["active"] == identity:
                    return
                active = self.state["active"]
                control_worker(self.data_dir, active, "recover", owners=["relay:" + identity])
                updated = {**self.state, "draining": [entry for entry in self.state.get("draining", []) if entry["id"] != identity]}
                atomic_json(self.state_path, updated)
                self.state = updated
                if not updated["draining"]:
                    control_worker(self.data_dir, active, "maintenance", enabled=True)
        except (OSError, ValueError, http.client.HTTPException):
            pass  # Leave the draining entry visible and retry; do not force-stop it.
        finally:
            with self.guard:
                self.retiring.discard(identity)

    def start_monitor(self):
        def run():
            while not self.monitor_stop.wait(1):
                with self.guard:
                    ready = [entry["id"] for entry in self.state.get("draining", [])
                             if entry["not_before"] <= time.time() and not self.counts.get(entry["id"], 0)
                             and entry["id"] not in self.retiring]
                    self.retiring.update(ready)
                for identity in ready:
                    threading.Thread(target=self._retire, args=(identity,), daemon=True).start()
                if time.monotonic() >= self.restart_after:
                    self.restart_after = time.monotonic() + 10
                    self._restart_exited_backend()
        self.monitor_thread = threading.Thread(target=run, daemon=True)
        self.monitor_thread.start()

    def _restart_exited_backend(self):
        with self.change_guard:
            identity = self.state["active"]
            try:
                healthy_worker(self.data_dir, identity)
                return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            try:
                runtime = json.loads((self.data_dir / "router-workers" / (identity + ".runtime.json")).read_text(encoding="utf8"))
                if process_alive(runtime.get("pid")):
                    return  # Never terminate a busy or unresponsive process.
                child = start_worker(self.data_dir, self.config, identity, self.port)
                deadline = time.monotonic() + 25
                while not self.monitor_stop.wait(.1) and time.monotonic() < deadline:
                    if child.poll() is not None:
                        return
                    try:
                        healthy_worker(self.data_dir, identity)
                        control_worker(self.data_dir, identity, "activate", public_port=self.port,
                                       maintenance=not self.state.get("draining"))
                        return
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                pass  # Retry later; the public listener stays alive throughout.

    def server_close(self):
        self.monitor_stop.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2)
        if self.exit_requested:
            self.exit_requested = False
            with self.guard:
                workers = {self.state["active"], *self.state.get("draining", [])}
            for identity in workers:
                try:
                    control_worker(self.data_dir, identity, "stop")
                except (OSError, ValueError, http.client.HTTPException):
                    pass
        super().server_close()


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def reject(self, code, data):
        discard_small_body(self)
        self.json(code, data)

    def _control(self, path):
        if not secrets.compare_digest(self.headers.get("X-Inspector-Control", ""), self.server.state["token"]):
            self.reject(403, {"error": "router_control_required"})
            return
        if path == "/__router__/status" and self.command == "GET":
            self.json(200, self.server.status())
            return
        if self.command != "POST":
            self.json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or not 0 <= length <= 8192:
            raise ValueError("invalid control body")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete control body")
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("invalid control body")
        if path == "/__router__/switch":
            self.json(200, self.server.switch(data.get("worker_id"), data.get("expected_active")))
        elif path == "/__router__/stop":
            with self.server.guard:
                if any(self.server.counts.values()):
                    self.json(409, {"error": "requests_in_flight"})
                    return
                self.server.stopping = True
            self.json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.json(404, {"error": "not_found"})

    def _forward(self, path):
        relay = path.startswith("/relay/")
        # Serialize settings writes with a cutover; inference streams stay independent.
        write_guard = self.server.change_guard if self.command not in {"GET", "HEAD"} and not relay else nullcontext()
        with write_guard:
            if path == "/api/stop" and self.server.status()["relay_requests"]:
                self.reject(409, {"error": "requests_in_flight"})
                return
            identity, worker = self.server.acquire(relay)
            connection = http.client.HTTPConnection("127.0.0.1", worker["port"], timeout=600)
            sent = False
            try:
                transfer = self.headers.get("Transfer-Encoding", "").lower()
                lengths = self.headers.get_all("Content-Length", [])
                length = int(lengths[0]) if lengths else 0
                if transfer not in {"", "chunked"} or (transfer and lengths) or len(lengths) > 1 or not 0 <= length <= MAX_BODY:
                    self.json(400, {"error": "invalid_request_framing"})
                    return
                tokens = {value.strip().lower() for value in self.headers.get("Connection", "").split(",")}
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for key, value in self.headers.items():
                    if key.lower() not in HOP | tokens | {"host", "content-length", "x-inspector-control"}:
                        connection.putheader(key, value)
                connection.putheader("Host", f"127.0.0.1:{worker['port']}")
                connection.putheader("Connection", "close")
                connection.putheader("Transfer-Encoding", "chunked") if transfer else connection.putheader("Content-Length", str(length))
                connection.endheaders()
                if transfer:
                    total = 0
                    while True:
                        line = self.rfile.readline(128)
                        size = int(line.split(b";", 1)[0].strip(), 16)
                        if size < 0 or total + size > MAX_BODY:
                            raise ValueError("request too large")
                        if not size:
                            trailers = 0
                            while True:
                                trailer = self.rfile.readline(8192)
                                trailers += len(trailer)
                                if trailers > 65536 or not trailer:
                                    raise ValueError("invalid trailers")
                                if trailer == b"\r\n":
                                    break
                            connection.send(b"0\r\n\r\n")
                            break
                        chunk = self.rfile.read(size)
                        if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                            raise ValueError("incomplete request body")
                        connection.send(("%x\r\n" % size).encode() + chunk + b"\r\n")
                        total += size
                else:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("incomplete request body")
                        connection.send(chunk)
                        remaining -= len(chunk)
                response = connection.getresponse()
                if path == "/api/status" and response.status == 200:
                    raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError("status too large")
                    status = json.loads(raw)
                    routing = self.server.status()
                    status.update(router_supported=True, router_version=ROUTER_VERSION,
                                  active_requests=routing["relay_requests"], relay_active_requests=routing["relay_requests"],
                                  draining_workers=len(routing["draining"]))
                    self.json(200, status)
                    return
                self.send_response(response.status, response.reason)
                response_tokens = {value.strip().lower() for value in response.getheader("Connection", "").split(",")}
                for key, value in response.getheaders():
                    if key.lower() not in HOP | response_tokens:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                sent = True
                if self.command != "HEAD":
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                if path == "/api/stop" and response.status == 200:
                    self.server.exit_requested = True
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            except (OSError, ValueError, http.client.HTTPException):
                if not sent:
                    self.json(502, {"error": "backend_unavailable", "message": "中转服务暂不可用，请稍后重试。"})
                # A submitted request is never replayed on another backend.
            finally:
                self.close_connection = True
                connection.close()
                self.server.release(identity, relay)

    def _handle(self):
        self.connection.settimeout(600)
        hosts = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in hosts or (origin and origin not in {"http://" + host for host in hosts}):
            self.reject(403, {"error": "local_origin_required"})
            return
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment or not self.path.startswith("/") or self.path.startswith("//"):
            self.reject(400, {"error": "invalid_request_path"})
            return
        path = target.path
        try:
            if path.startswith("/__router__/"):
                self._control(path)
            elif path.startswith("/__worker__/") or path == "/api/upgrade/stop":
                self.reject(403, {"error": "use_upgrade_executable"})
            elif path == "/health" and self.command == "GET":
                worker = worker_info(self.server.data_dir, self.server.state["active"])
                self.json(200, {"app": "codex-packet-inspector", "ok": True, "version": worker["version"],
                                "router_version": ROUTER_VERSION, "router_protocol": PROTOCOL})
            else:
                self._forward(path)
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as exc:
            self.json(409, {"error": type(exc).__name__})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


def startup_port(args, state):
    path = args.data_dir / "entry-port.json"
    port = args.port if args.port is not None else (json.loads(path.read_text(encoding="utf-8-sig"))["port"] if path.exists() else state["port"])
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid entry port")
    return port


def ensure_entry(args, state):
    """A foreground launch must restore the router, never start a direct backend."""
    def ready(port):
        health = local_json(port, "/health", timeout=2)
        status = local_json(port, "/api/status", timeout=3)
        if (health.get("app") != "codex-packet-inspector" or health.get("router_protocol") != PROTOCOL
                or Path(status["data_dir"]).resolve() != args.data_dir.resolve()
                or Path(status["profile"]["config_path"]).resolve() != args.config.resolve()):
            raise ValueError("entry port belongs to another service; run the upgrade EXE")
        return port
    try:
        return ready(state["port"])
    except (OSError, http.client.HTTPException):
        pass
    port = startup_port(args, state)
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "packet_inspector.router"]
    child = subprocess.Popen(command + ["--background", "--tray", "--port", str(port),
                                       "--data-dir", str(args.data_dir), "--config", str(args.config)],
                             env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            return ready(port)
        except (OSError, http.client.HTTPException):
            if child.poll() not in {None, 0}:
                raise RuntimeError("entry startup failed") from None
            time.sleep(.1)
    raise RuntimeError("entry startup timed out")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".codex-packet-inspector")
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--port", type=int)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--tray", action="store_true")
    args = parser.parse_args(argv)
    state = json.loads((args.data_dir / "router-state.json").read_text(encoding="utf8"))
    if not args.background:
        port = ensure_entry(args, state)
        state = json.loads((args.data_dir / "router-state.json").read_text(encoding="utf8"))
        worker = worker_info(args.data_dir, state["active"])
        subprocess.Popen([worker["executable"], "--data-dir", str(args.data_dir), "--config", str(args.config),
                          "--port", str(port)], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return 0
    port = startup_port(args, state)
    try:
        server = RouterServer(("127.0.0.1", port), args.data_dir, args.config)
    except OSError:
        health = local_json(port, "/health", timeout=2)
        status = local_json(port, "/api/status", timeout=2)
        if (health.get("router_protocol") == PROTOCOL and Path(status["data_dir"]).resolve() == args.data_dir.resolve()
                and Path(status["profile"]["config_path"]).resolve() == args.config.resolve()):
            return 0
        raise RuntimeError("entry port is occupied by another profile") from None
    identity = state["active"]
    tray = None
    try:
        try:
            healthy_worker(args.data_dir, identity)
        except (OSError, ValueError, http.client.HTTPException):
            process = start_worker(args.data_dir, args.config, identity, server.port)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("worker startup failed")
                try:
                    healthy_worker(args.data_dir, identity)
                    break
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(.1)
            else:
                raise RuntimeError("worker startup timed out")
        control_worker(args.data_dir, identity, "activate", public_port=server.port, maintenance=not state.get("draining"))
        server.state["port"] = server.port
        atomic_json(server.state_path, server.state)
        atomic_json(args.data_dir / "runtime.json", {"pid": os.getpid(), "port": server.port,
                    "version": worker_info(args.data_dir, identity)["version"], "router_version": ROUTER_VERSION})
        server.start_monitor()
        if args.tray and os.name == "nt":
            from packet_inspector.tray import NativeTray
            def open_window():
                worker = worker_info(args.data_dir, server.state["active"])
                subprocess.Popen([worker["executable"], "--port", str(server.port), "--data-dir", str(args.data_dir),
                                  "--config", str(args.config)], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def exit_app():
                try:
                    local_json(server.port, "/api/stop", {})
                except (OSError, ValueError, http.client.HTTPException) as error:
                    open_window()
                    import ctypes
                    message = ("仍有请求正在转发，服务继续运行。请在请求完成后从托盘退出。"
                               if str(error) == "control HTTP 409" else "退出未完成，中转服务继续运行。请稍后重试。")
                    ctypes.windll.user32.MessageBoxW(0, message, "Codex 请求优化器", 0x40)
            tray = NativeTray(open_window, exit_app).start()
        server.serve_forever(poll_interval=.1)
    finally:
        if tray:
            tray.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        try:
            folder = Path(sys.argv[sys.argv.index("--data-dir") + 1]) if "--data-dir" in sys.argv else Path.home() / ".codex-packet-inspector"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "router-error.txt").write_text(type(error).__name__ + ": " + str(error), encoding="utf8")
        except (OSError, ValueError, IndexError):
            pass
        if getattr(sys, "frozen", False) and "--background" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "入口服务启动失败：" + str(error)[:350], "Codex Request Optimizer", 0x10)
        raise
