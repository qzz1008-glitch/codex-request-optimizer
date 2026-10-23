"""Tray exit must require a desktop choice and preserve service on failure."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from packet_inspector.desktop_ui import DesktopUI
from packet_inspector.exit_flow import ExitError, ExitRequests, REQUEST_FILE, perform_exit, request_exit


class Control:
    def __init__(self):
        self.active = True
        self.can_restore = True
        self.unknown = False
        self.inflight = 0
        self.stopped = False
        self.restore_fails = False
        self.restore_ignored = False
        self.stop_busy = False
        self.calls = []

    def __call__(self, path, data=None):
        self.calls.append((path, data))
        if path == "/api/status":
            return 200, {"version": "test", "active_requests": self.inflight,
                         "profile": {"active": self.active, "can_restore": self.can_restore,
                                     "config_error": "test" if self.unknown else None}}
        if path == "/api/link":
            if self.restore_fails:
                return 400, {"error": "restore_failed"}
            if not self.restore_ignored:
                self.active = False
            return 200, {"active": self.active}
        if path == "/api/stop":
            if self.stop_busy:
                return 409, {"error": "requests_in_flight"}
            self.stopped = True
            return 200, {"stopped": True}
        return 404, {"error": "not_found"}


class ExitFlowTests(unittest.TestCase):
    def test_restore_is_verified_before_service_stops(self):
        control = Control()
        self.assertEqual(perform_exit(control, "restore"), {"stopped": True, "restored": True})
        self.assertEqual(control.calls, [("/api/status", None), ("/api/link", {"action": "restore"}),
                                         ("/api/status", None), ("/api/stop", {})])
        self.assertFalse(control.active)
        self.assertTrue(control.stopped)

    def test_direct_exit_preserves_provider_configuration(self):
        control = Control()
        perform_exit(control, "direct")
        self.assertTrue(control.active)
        self.assertTrue(control.stopped)
        self.assertNotIn("/api/link", [path for path, _ in control.calls])

    def test_disconnected_exit_does_not_restore_again(self):
        control = Control()
        control.active = False
        control.can_restore = False
        perform_exit(control, "restore")
        self.assertEqual([path for path, _ in control.calls], ["/api/status", "/api/stop"])

    def test_restore_failures_never_stop_service(self):
        for setting in ("unknown", "restore_fails", "restore_ignored", "can_restore"):
            with self.subTest(setting=setting):
                control = Control()
                setattr(control, setting, setting != "can_restore")
                with self.assertRaises(ExitError):
                    perform_exit(control, "restore")
                self.assertFalse(control.stopped)
                self.assertNotIn("/api/stop", [path for path, _ in control.calls])

    def test_active_requests_are_never_interrupted(self):
        for action in ("restore", "direct"):
            control = Control()
            control.inflight = 1
            with self.assertRaisesRegex(ExitError, "仍有请求"):
                perform_exit(control, action)
            self.assertEqual(control.calls, [("/api/status", None)])
            self.assertFalse(control.stopped)
            self.assertTrue(control.active)

    def test_request_arriving_during_restore_is_not_forced_closed(self):
        control = Control()
        control.stop_busy = True
        with self.assertRaisesRegex(ExitError, "仍有请求"):
            perform_exit(control, "restore")
        self.assertFalse(control.stopped)
        self.assertFalse(control.active)

    def test_unknown_actions_do_not_call_relay(self):
        control = Control()
        with self.assertRaises(ExitError):
            perform_exit(control, "force")
        self.assertEqual(control.calls, [])

    def test_disconnected_auto_exit_verifies_current_status(self):
        control = Control()
        control.active = False
        perform_exit(control, "disconnected")
        self.assertTrue(control.stopped)
        self.assertEqual(control.calls, [("/api/status", None), ("/api/stop", {})])

    def test_reconnection_requires_confirmation_before_auto_exit(self):
        control = Control()
        with self.assertRaisesRegex(ExitError, "接入状态已变化"):
            perform_exit(control, "disconnected")
        self.assertFalse(control.stopped)
        self.assertEqual(control.calls, [("/api/status", None)])


class ExitIntentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_tray_only_opens_window_and_intent_is_cleaned_after_claim(self):
        opened = []
        request_exit(self.folder, lambda: opened.append(True))
        self.assertEqual(opened, [True])
        requests = ExitRequests(self.folder)
        identity = requests.current()
        self.assertRegex(identity, r"^[0-9a-f]{32}$")
        self.assertEqual(requests.current(), identity)
        self.assertEqual(list(self.folder.iterdir()), [])
        requests.begin(identity)
        with self.assertRaisesRegex(ValueError, "正在处理"):
            requests.begin(identity)
        requests.finish(completed=False)
        self.assertEqual(requests.current(), identity)
        requests.begin(identity)
        requests.finish(completed=True)
        self.assertIsNone(requests.current())
        with self.assertRaisesRegex(ValueError, "已失效"):
            requests.begin(identity)

    def test_failed_window_launch_removes_intent(self):
        def fail():
            raise OSError("window unavailable")
        with self.assertRaises(OSError):
            request_exit(self.folder, fail)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_stale_invalid_and_oversized_intents_are_discarded(self):
        path = self.folder / REQUEST_FILE
        for raw in ("not json", "x" * 2048, "[]", json.dumps({"id": "a" * 32, "created": time.time() - 301}),
                    json.dumps({"id": "a" * 32, "created": time.time() + 30}),
                    json.dumps({"id": "invalid", "created": time.time()})):
            path.write_text(raw, encoding="utf8")
            self.assertIsNone(ExitRequests(self.folder).current())
            self.assertEqual(list(self.folder.iterdir()), [])


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length)) if length else None
        status, value = self.server.control(self.path, data)
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = handle_request
    do_POST = handle_request


class DesktopExitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.control = Control()
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        self.relay.control = self.control
        self.thread = threading.Thread(target=self.relay.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.ui = DesktopUI(self.relay.server_port, exit_data_dir=self.folder).start()

    def tearDown(self):
        self.ui.close()
        self.relay.shutdown()
        self.relay.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=3)
        try:
            body = None if data is None else json.dumps(data)
            connection.request("GET" if data is None else "POST", path, body,
                               {"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def intent(self):
        request_exit(self.folder, lambda: None)
        status, data = self.request("/api/status")
        self.assertEqual(status, 200)
        return data["ui_exit_request"]

    def test_fresh_tray_intent_is_required_and_cross_origin_is_rejected(self):
        self.assertEqual(self.request("/api/exit", {"request_id": "a" * 32, "action": "direct"})[0], 409)
        identity = self.intent()
        self.assertEqual(self.request("/api/exit", {"request_id": "b" * 32, "action": "direct"})[0], 409)
        self.assertEqual(self.request("/api/exit", {"request_id": identity, "action": "direct"},
                                      {"Origin": "https://example.com"})[0], 403)
        self.assertFalse(self.control.stopped)
        self.assertTrue(self.control.active)
        self.assertEqual(self.request("/api/stop", {})[0], 404)

    def test_cancel_leaves_configuration_and_service_unchanged(self):
        identity = self.intent()
        self.control.calls.clear()
        self.assertEqual(self.request("/api/exit", {"request_id": identity, "action": "cancel"}),
                         (200, {"cancelled": True}))
        self.assertEqual(self.control.calls, [])
        self.assertIsNone(self.request("/api/status")[1]["ui_exit_request"])

    def test_restore_error_is_retryable_without_stopping_service(self):
        identity = self.intent()
        self.control.restore_fails = True
        self.assertEqual(self.request("/api/exit", {"request_id": identity, "action": "restore"})[0], 502)
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.request("/api/status")[1]["ui_exit_request"], identity)
        self.control.restore_fails = False
        self.assertEqual(self.request("/api/exit", {"request_id": identity, "action": "restore"})[0], 200)
        self.assertFalse(self.control.active)
        self.assertTrue(self.control.stopped)

    def test_direct_exit_preserves_configuration_over_http(self):
        identity = self.intent()
        self.assertEqual(self.request("/api/exit", {"request_id": identity, "action": "direct"})[0], 200)
        self.assertTrue(self.control.active)
        self.assertTrue(self.control.stopped)

    def test_preview_cannot_consume_tray_intents_or_stop_relay(self):
        self.ui.close()
        self.ui = DesktopUI(self.relay.server_port, live_reload=True).start()
        request_exit(self.folder, lambda: None)
        self.assertNotIn("ui_exit_request", self.request("/api/status")[1])
        self.assertTrue((self.folder / REQUEST_FILE).is_file())
        self.assertEqual(self.request("/api/exit", {"request_id": "a" * 32, "action": "direct"})[0], 404)
        self.assertFalse(self.control.stopped)

    def test_read_only_ui_cannot_consume_or_confirm_exit(self):
        self.ui.read_only = True
        request_exit(self.folder, lambda: None)
        self.assertNotIn("ui_exit_request", self.request("/api/status")[1])
        self.assertTrue((self.folder / REQUEST_FILE).is_file())
        self.assertEqual(self.request("/api/exit", {"request_id": "a" * 32, "action": "direct"})[0], 409)
        self.assertFalse(self.control.stopped)

    def test_invalid_action_leaves_pending_exit_available(self):
        identity = self.intent()
        for value in ([], {"request_id": identity, "action": []}, {"request_id": identity, "action": "force"}):
            self.assertEqual(self.request("/api/exit", value)[0], 400)
        self.assertEqual(self.request("/api/status")[1]["ui_exit_request"], identity)
        self.assertFalse(self.control.stopped)


class IsolatedRelayExitTests(unittest.TestCase):
    def test_restore_and_exit_with_real_standard_provider_config(self):
        from packet_inspector.server import InspectorServer
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            config = folder / "config.toml"
            config.write_text('model="isolated-exit-test"\n', encoding="utf8")
            data = folder / "data"
            relay = InspectorServer(("127.0.0.1", 0), data, config, "http://127.0.0.1:9/v1")
            thread = threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            ui = DesktopUI(relay.port, exit_data_dir=data).start()
            try:
                relay.sync_providers()
                self.assertTrue(relay.link.profile()["active"])
                request_exit(data, lambda: None)

                def request(path, value=None):
                    connection = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=3)
                    try:
                        connection.request("GET" if value is None else "POST", path,
                                           None if value is None else json.dumps(value),
                                           {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        return json.loads(response.read())
                    finally:
                        connection.close()

                identity = request("/api/status")["ui_exit_request"]
                result = request("/api/exit", {"request_id": identity, "action": "restore"})
                self.assertTrue(result["stopped"])
                self.assertFalse(relay.link.profile()["active"])
                self.assertFalse(relay.settings["auto_detect_providers"])
                self.assertEqual(config.read_text(encoding="utf8").strip(), 'model="isolated-exit-test"')
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(list(data.glob("desktop-exit-request*")), [])
            finally:
                ui.close()
                if thread.is_alive():
                    relay.shutdown()
                    thread.join(timeout=2)
                relay.server_close()


if __name__ == "__main__":
    unittest.main()
