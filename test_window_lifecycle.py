"""Tray close and explicit exit must keep their different service lifetimes."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector.window_lifecycle import WindowLifecycle
import test_router


class Callbacks(list):
    def __iadd__(self, callback):
        self.append(callback)
        return self


class WindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock()
        self.window.events = SimpleNamespace(closing=Callbacks(), closed=Callbacks())
        self.window.native = SimpleNamespace(WindowState="Normal")
        self.marked = patch("packet_inspector.window_lifecycle.window_exit_requested", return_value=False)
        self.exit_requested = self.marked.start()
        self.addCleanup(self.marked.stop)
        self.lifecycle = WindowLifecycle(self.window, lambda: True)

    def test_close_keeps_window_alive_and_open_restores_same_window(self):
        self.assertFalse(self.window.events.closing[0]())
        self.window.hide.assert_called_once()
        self.window.destroy.assert_not_called()
        self.assertFalse(self.lifecycle.closed.is_set())
        self.assertTrue(self.lifecycle.hidden.is_set())
        self.window.native.WindowState = "Minimized"
        self.lifecycle.show()
        self.window.restore.assert_called_once()
        self.window.show.assert_called_once()
        self.assertFalse(self.lifecycle.hidden.is_set())

    def test_no_tray_keeps_window_reachable_in_taskbar(self):
        self.lifecycle.can_hide = lambda: False
        self.assertFalse(self.lifecycle.on_closing())
        self.window.hide.assert_not_called()
        self.window.minimize.assert_called_once()

    def test_service_exit_closes_window_instead_of_hiding_it(self):
        self.lifecycle.exit()
        self.assertTrue(self.lifecycle.on_closing())
        self.window.destroy.assert_called_once()
        self.window.hide.assert_not_called()
        self.window.events.closed[0]()
        self.lifecycle.show()
        self.window.show.assert_not_called()

    def test_upgrade_mark_allows_graceful_close_without_touching_service(self):
        self.exit_requested.return_value = True
        self.assertTrue(self.lifecycle.on_closing())
        self.window.hide.assert_not_called()
        self.window.minimize.assert_not_called()


class TrayServiceExitTests(unittest.TestCase):
    def test_exit_keeps_provider_configuration_and_stops_all_managed_workers(self):
        fixture = test_router.RouterTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.first.link.activate(f"http://127.0.0.1:{fixture.router.port}/v1",
                                    f"http://127.0.0.1:{fixture.upstream.server_port}")
        self.assertTrue(fixture.first.link.profile()["active"])
        previous = fixture.config.read_bytes()
        old = fixture.worker("2" * 32)
        fixture.router.state["draining"] = [old.worker_id]
        with patch("packet_inspector.router.control_worker") as control:
            self.assertEqual(fixture.api("/api/stop", {})[0], 200)
            fixture.router.shutdown()
            fixture.router.server_close()
        self.assertEqual(fixture.config.read_bytes(), previous)
        self.assertTrue(fixture.first.upgrade_draining)
        self.assertEqual({call.args[1] for call in control.call_args_list},
                         {fixture.first.worker_id, old.worker_id})
        self.assertTrue(all(call.args[2] == "stop" for call in control.call_args_list))


if __name__ == "__main__":
    unittest.main()
