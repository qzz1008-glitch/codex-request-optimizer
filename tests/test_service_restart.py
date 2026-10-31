"""Tray confirmation and a real, isolated worker restart through the fixed entry."""
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from packet_inspector import VERSION
from packet_inspector.router import PROTOCOL, RouterServer, atomic_json, control_worker, local_json
from packet_inspector.service_restart import _dialog, _target, restart_from_tray


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path, data=None, token=""):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET" if data is None else "POST", path,
                           None if data is None else json.dumps(data).encode(),
                           {"Content-Type": "application/json", "X-Inspector-Control": token})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class RestartConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.identity = "1" * 32
        atomic_json(self.folder / "router-state.json", {"active": self.identity})
        atomic_json(self.folder / "router-workers" / (self.identity + ".json"),
                    {"id": self.identity, "port": 49123, "token": "synthetic-token"})
        self.runtime = self.folder / "router-workers" / (self.identity + ".runtime.json")
        atomic_json(self.runtime, {"pid": 12345, "port": 49123})

    def test_cancel_and_window_close_do_not_call_service_control(self):
        with patch("packet_inspector.service_restart._dialog", return_value=False) as dialog, \
                patch("packet_inspector.service_restart.control_worker") as control:
            self.assertEqual(restart_from_tray(self.folder), {"ok": True, "cancelled": True})
        control.assert_not_called()
        self.assertIn("正在进行的请求可能失败", dialog.call_args.args[0])
        self.assertEqual(dialog.call_args.kwargs, {"confirm": True})

    def test_confirmation_cannot_restart_a_worker_replaced_while_the_dialog_was_open(self):
        def dialog(*_args, confirm=False):
            if confirm:
                atomic_json(self.runtime, {"pid": 12346, "port": 49123})
            return confirm
        with patch("packet_inspector.service_restart._dialog", side_effect=dialog), \
                patch("packet_inspector.service_restart.control_worker") as control:
            self.assertFalse(restart_from_tray(self.folder)["ok"])
        control.assert_not_called()

    def test_native_confirmation_defaults_to_no_and_only_yes_authorizes_restart(self):
        user = Mock()
        with patch.object(ctypes, "windll", Mock(user32=user), create=True):
            for result in (0, 2, 7, 6):
                user.MessageBoxW.return_value = result
                self.assertEqual(_dialog("message", "title", confirm=True), result == 6)
            flags = user.MessageBoxW.call_args.args[3]
            self.assertEqual(flags & 0xF, 4)  # Yes / No
            self.assertEqual(flags & 0x300, 0x100)  # No is the default button.


class IsolatedWorkerRestartTests(unittest.TestCase):
    def test_confirmation_restarts_an_inflight_worker_and_keeps_entry_and_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            identity = "2" * 32
            config = folder / "config.toml"
            config.write_text('model="test"\n', encoding="utf8")
            atomic_json(folder / "settings.json", {"auto_detect_providers": False, "ui_language": "en",
                                                  "zstd_level": 6, "capacity_retry_enabled": False})
            holding, release = threading.Event(), threading.Event()
            success = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1}}}\n\n'
            class Upstream(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if json.loads(body).get("input") == "hold":
                        holding.set()
                        release.wait(30)
                    try:
                        self.wfile.write(success)
                    except (OSError, ConnectionError):
                        pass
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream.daemon_threads = True
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            private_port = free_port()
            atomic_json(folder / "router-workers" / (identity + ".json"),
                        {"id": identity, "port": private_port, "token": "synthetic-control", "version": VERSION,
                         "desktop_tray": False, "executable": sys.executable})
            atomic_json(folder / "router-state.json", {"protocol": PROTOCOL, "active": identity, "draining": [],
                                                       "config": str(config), "token": "synthetic-router"})
            router = RouterServer(("127.0.0.1", 0), folder, config)
            router_thread = threading.Thread(target=router.serve_forever, daemon=True)
            children = []
            hold_thread = None
            def start_child(_folder, _config, worker_id, public_port):
                child = subprocess.Popen([sys.executable, "-m", "packet_inspector", "--background",
                    "--worker-id", worker_id, "--public-port", str(public_port), "--port", str(private_port),
                    "--data-dir", str(folder), "--config", str(config),
                    "--upstream", "http://127.0.0.1:" + str(upstream.server_port)],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                children.append(child)
                return child
            def wait_until(predicate):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        if predicate():
                            return
                    except (OSError, ValueError, KeyError, http.client.HTTPException):
                        pass
                    time.sleep(.05)
                self.fail("Isolated worker did not become ready")
            router_thread.start()
            try:
                with patch("packet_inspector.router.start_worker", side_effect=start_child):
                    first = start_child(folder, config, identity, router.port)
                    wait_until(lambda: local_json(private_port, "/health").get("worker_id") == identity)
                    control_worker(folder, identity, "activate", public_port=router.port)
                    settings_before = json.loads((folder / "settings.json").read_text(encoding="utf8"))
                    endpoint = "/relay/" + settings_before["relay_token"]
                    self.assertEqual(request(router.port, endpoint + "/responses", {"input": "before"}), (200, success))
                    config_before = config.read_bytes()
                    self.assertEqual(request(private_port, "/__worker__/control", {"action": "restart", "expected_pid": first.pid})[0], 403)
                    self.assertEqual(request(private_port, "/__worker__/control", {"action": "restart", "expected_pid": first.pid + 1}, "synthetic-control")[0], 409)
                    with patch("packet_inspector.service_restart._dialog", return_value=False):
                        self.assertEqual(restart_from_tray(folder), {"ok": True, "cancelled": True})
                    self.assertIsNone(first.poll())
                    def hold_request():
                        try:
                            request(router.port, endpoint + "/responses", {"input": "hold"})
                        except (OSError, http.client.HTTPException):
                            pass  # The confirmed restart may interrupt this response.
                    hold_thread = threading.Thread(target=hold_request, daemon=True)
                    hold_thread.start()
                    self.assertTrue(holding.wait(5))
                    self.assertGreater(router.status()["relay_requests"], 0)
                    router.start_monitor()
                    with patch("packet_inspector.service_restart._dialog", return_value=True) as dialog:
                        self.assertEqual(restart_from_tray(folder), {"ok": True, "restarting": True})
                    self.assertIn("requests in progress may fail", dialog.call_args.args[0])
                    self.assertEqual(local_json(router.port, "/health")["app"], "codex-packet-inspector")
                    wait_until(lambda: first.poll() is not None)
                    hold_thread.join(timeout=3)
                    self.assertFalse(hold_thread.is_alive(), "The interrupted request must end before recovery")
                    wait_until(lambda: len(children) == 2 and _target(folder)[1] != first.pid
                               and local_json(private_port, "/api/status").get("worker_state") == "active")
                    self.assertEqual(_target(folder), (identity, children[1].pid))
                    self.assertEqual(request(router.port, endpoint + "/responses", {"input": "after"}), (200, success))
                    self.assertEqual(config.read_bytes(), config_before)
                    self.assertEqual(json.loads((folder / "settings.json").read_text(encoding="utf8")), settings_before)
            finally:
                release.set()
                router.shutdown()
                router.server_close()
                router_thread.join(timeout=3)
                # Only subprocesses created by this isolated test are terminated.
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)
                    child.stderr.close()
                if hold_thread:
                    hold_thread.join(timeout=3)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
