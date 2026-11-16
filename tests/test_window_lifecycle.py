"""Tray close and explicit exit must keep their different service lifetimes."""
from __future__ import annotations

from types import SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from packet_inspector.tray_panel import TrayPanel
from packet_inspector.window_lifecycle import WindowLifecycle
from tests import test_router


class Callbacks(list):
    def __iadd__(self, callback):
        self.append(callback)
        return self


class WindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock()
        self.window.events = SimpleNamespace(closing=Callbacks(), closed=Callbacks(), before_show=Callbacks())
        self.window.native = Mock(WindowState="Normal", InvokeRequired=False, IsDisposed=False, Disposing=False)
        platform = patch("packet_inspector.window_lifecycle.os", SimpleNamespace(name="nt"))
        platform.start()
        self.addCleanup(platform.stop)
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
        self.window.native.BringToFront.assert_called_once()
        self.window.native.Activate.assert_called_once()
        self.assertFalse(self.lifecycle.hidden.is_set())

    def test_covered_maximized_window_is_activated_without_changing_its_size(self):
        self.window.native.WindowState = "Maximized"
        calls = Mock()
        calls.attach_mock(self.window.show, "show")
        calls.attach_mock(self.window.native.BringToFront, "front")
        calls.attach_mock(self.window.native.Activate, "activate")
        self.lifecycle.show()
        self.assertEqual([call[0] for call in calls.mock_calls], ["show", "front", "activate"])
        self.window.restore.assert_not_called()
        self.assertEqual(self.window.native.WindowState, "Maximized")

    def queue_show(self):
        self.window.native.InvokeRequired = True
        with patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda callback: callback)}):
            self.lifecycle.show()
        self.window.native.BeginInvoke.assert_called_once()
        self.window.show.assert_not_called()
        self.window.native.Activate.assert_not_called()
        return self.window.native.BeginInvoke.call_args.args[0]

    def test_tray_thread_queues_the_entire_restore_and_activation_on_the_ui_thread(self):
        self.window.native.WindowState = "Minimized"
        queued = self.queue_show()
        self.window.restore.assert_not_called()
        queued()
        self.window.restore.assert_called_once()
        self.window.show.assert_called_once()
        self.window.native.Activate.assert_called_once()

    def test_custom_menu_open_exit_and_restart_all_activate_the_same_main_window(self):
        panel = TrayPanel(Mock(), "http://127.0.0.1/", ".", self.lifecycle.show)

        def confirm_in_main_window(_folder, show):
            show()
            return {"ok": True}

        with patch("packet_inspector.exit_flow.request_exit", side_effect=confirm_in_main_window), \
                patch("packet_inspector.service_restart.restart_from_tray", side_effect=confirm_in_main_window):
            for action in (panel.open_main, panel.exit_app, panel.restart_service):
                with self.subTest(action=action.__name__):
                    self.window.native.Activate.reset_mock()
                    self.assertEqual(action(), {"ok": True})
                    self.window.native.Activate.assert_called_once()
                    self.window.destroy.assert_not_called()

    def test_queued_show_does_not_reopen_an_exiting_closed_or_disposed_window(self):
        for state in ("exiting", "closed", "disposed", "disposing", "upgrade"):
            with self.subTest(state=state):
                self.window.native.BeginInvoke.reset_mock()
                queued = self.queue_show()
                if state in ("exiting", "closed"):
                    getattr(self.lifecycle, state).set()
                elif state == "upgrade":
                    self.exit_requested.return_value = True
                else:
                    setattr(self.window.native, "IsDisposed" if state == "disposed" else "Disposing", True)
                queued()
                self.window.show.assert_not_called()
                self.window.native.Activate.assert_not_called()
                self.lifecycle.exiting.clear()
                self.lifecycle.closed.clear()
                self.window.native.IsDisposed = self.window.native.Disposing = False
                self.exit_requested.return_value = False

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

    def test_exit_closes_once_when_confirmation_and_health_poll_overlap(self):
        start = threading.Barrier(2)

        def exit_window():
            start.wait(1)
            self.lifecycle.exit()

        threads = [threading.Thread(target=exit_window, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.window.destroy.assert_called_once()
        self.lifecycle.show()
        self.window.show.assert_not_called()

    def test_exit_cancels_loading_wait_so_a_starting_window_can_upgrade(self):
        result = []
        loading = threading.Event()
        waiter = threading.Thread(target=lambda: result.append(self.lifecycle.wait_loaded(loading)), daemon=True)
        waiter.start()
        self.lifecycle.exit()
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [False])

    def test_a_failed_native_close_can_be_retried(self):
        self.window.destroy.side_effect = [RuntimeError("Window not ready"), None]
        with self.assertRaises(RuntimeError):
            self.lifecycle.exit()
        self.assertFalse(self.lifecycle.exiting.is_set())
        self.lifecycle.exit()
        self.assertTrue(self.lifecycle.exiting.is_set())
        self.assertEqual(self.window.destroy.call_count, 2)

    def test_loading_wait_accepts_a_ready_page_but_not_a_closed_window(self):
        loaded = threading.Event()
        loaded.set()
        self.assertTrue(self.lifecycle.wait_loaded(loaded))
        self.window.events.closed[0]()
        self.assertFalse(self.lifecycle.wait_loaded(loaded))


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
