"""The tray reuses the desktop process, owns no service, and keeps a small bridge."""
import tempfile
import threading
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector.tray import NativeTray, NATIVE_MENU_MESSAGE
from packet_inspector.tray_panel import PanelAPI, TrayPanel, popup_position, resize_panel_window
from tests.test_window_lifecycle import Callbacks


class NativePanelResizeTests(unittest.TestCase):
    def test_resize_uses_dpi_and_native_ui_thread_without_showing_or_activating_the_window(self):
        user, window = Mock(), Mock()
        user.GetDpiForWindow.return_value = 144
        window.native.Handle.ToInt64.return_value = 123
        window.native.InvokeRequired = True
        window.native.Invoke.side_effect = lambda callback: callback()
        with patch("packet_inspector.tray_panel.os.name", "nt"), \
                patch("packet_inspector.tray_panel.ctypes.WinDLL", return_value=user), \
                patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda callback: callback)}):
            resize_panel_window(window, 344, 258)
        user.SetWindowPos.assert_called_once_with(123, None, 0, 0, 516, 387, 0x02 | 0x04 | 0x10)
        window.native.Invoke.assert_called_once()
        window.native.PerformLayout.assert_called_once()
        window.resize.assert_not_called()


class TrayPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.window = Mock()
        self.window.events = SimpleNamespace(loaded=Callbacks(), closing=Callbacks(), closed=Callbacks())
        self.factory, self.open_main = Mock(return_value=self.window), Mock()
        self.panel = TrayPanel(self.factory, "http://127.0.0.1:12345/", self.root, self.open_main)
        for name in ("popup_anchor", "place_panel", "configure_panel", "resize_panel_window", "popup_interaction_at", "show_panel_window"):
            patcher = patch("packet_inspector.tray_panel." + name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.popup_anchor.return_value = (1900, 1040), (0, 0, 1920, 1040)
        self.popup_interaction_at.return_value = None
        self.show_panel_window.side_effect = lambda window, **_: window.show()
        self.addCleanup(self.panel.destroy)

    def load(self):
        self.panel.show()
        self.window.events.loaded[0]()

    def test_repeated_right_clicks_reuse_one_frameless_window_and_no_service(self):
        self.load()
        self.panel.dismiss()
        self.panel.show()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.factory.call_args.args[1], "http://127.0.0.1:12345/tray.html")
        self.assertTrue(self.factory.call_args.kwargs["frameless"])
        self.assertTrue(self.factory.call_args.kwargs["hidden"])
        self.assertIsInstance(self.factory.call_args.kwargs["js_api"], PanelAPI)
        self.assertTrue(self.panel.panel_state()["visible"])
        self.open_main.assert_not_called()
        self.assertEqual(self.configure_panel.call_count, 1)

    def test_preload_stays_hidden_and_is_reused_on_first_click(self):
        self.panel.prepare()
        self.window.events.loaded[0]()
        self.assertFalse(self.panel.panel_state()["visible"])
        self.window.show.assert_not_called()
        self.open_main.assert_not_called()
        self.panel.show()
        self.window.show.assert_called_once()
        self.assertEqual(self.factory.call_count, 1)

    def test_native_show_and_hide_do_not_wait_for_a_blocked_page(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_page(*args):
            entered.set()
            release.wait(3)
        self.window.evaluate_js.side_effect = slow_page
        self.panel.prepare()
        self.window.events.loaded[0]()
        self.assertTrue(entered.wait(1))
        def click():
            self.panel.show()
            self.panel.dismiss()
            returned.set()
        caller = threading.Thread(target=click, daemon=True)
        caller.start()
        try:
            self.assertTrue(returned.wait(.2), "native popup waited for the page")
            self.window.show.assert_called_once()
            self.window.hide.assert_called_once()
        finally:
            release.set()
            caller.join(2)

    def test_preload_failure_does_not_open_main_but_a_click_failure_does(self):
        self.factory.side_effect = OSError("WebView unavailable")
        self.panel.prepare()
        self.open_main.assert_not_called()
        self.panel.show()
        self.open_main.assert_called_once()

    def test_hot_page_reload_preserves_visibility_and_does_not_reconfigure_native_hooks(self):
        self.load()
        self.panel.panel_ready()
        self.assertTrue(self.panel.panel_state()["visible"])
        self.assertEqual(self.configure_panel.call_count, 1)
        self.panel.dismiss()
        self.window.show.reset_mock()
        self.window.events.loaded[0]()
        self.assertFalse(self.panel.panel_state()["visible"])
        self.window.show.assert_not_called()

    def test_close_during_creation_or_loading_never_resurrects_an_upgrade_window(self):
        self.panel.show()
        self.panel.destroy()
        self.window.events.loaded[0]()
        self.panel.show()
        self.window.show.assert_not_called()
        self.window.destroy.assert_called_once()
        self.panel.destroy()
        self.window.destroy.assert_called_once()

    def test_upgrade_during_the_native_window_factory_closes_the_late_window(self):
        def create(*args, **kwargs):
            self.panel.destroy()
            return self.window
        self.factory.side_effect = create
        self.panel.show()
        self.window.destroy.assert_called_once()
        self.window.show.assert_not_called()
        self.assertFalse(self.panel.panel_state()["visible"])

    def test_focus_loss_and_alt_f4_hide_the_panel_without_running_javascript_on_the_ui_thread(self):
        self.load()
        hidden = threading.Event()
        self.window.hide.side_effect = hidden.set
        self.assertFalse(self.window.events.closing[0]())
        self.assertTrue(hidden.wait(2))
        self.assertFalse(self.panel.panel_state()["visible"])
        self.window.destroy.assert_not_called()
        self.panel.show()
        hidden.clear()
        self.panel._deactivate()
        self.assertTrue(hidden.wait(2))
        self.panel.destroy()
        self.assertTrue(self.window.events.closing[0]())

    def test_open_and_exit_use_existing_main_window_and_confirmation_flow(self):
        self.load()
        self.assertEqual(self.panel.open_main(), {"ok": True})
        self.open_main.assert_called_once()
        self.window.hide.assert_called_once()
        self.panel.show()
        with patch("packet_inspector.exit_flow.request_exit") as request:
            self.assertEqual(self.panel.exit_app(), {"ok": True})
        request.assert_called_once_with(self.root, self.open_main)
        self.assertFalse(self.panel.panel_state()["visible"])

    def test_failed_confirmation_restores_panel_instead_of_leaving_an_invisible_error(self):
        self.load()
        with patch("packet_inspector.exit_flow.request_exit", side_effect=OSError("unavailable")):
            self.assertEqual(self.panel.exit_app(), {"ok": False})
        self.assertTrue(self.panel.panel_state()["visible"])

    def test_only_fixed_bridge_actions_are_public_and_resize_is_bounded(self):
        self.load()
        self.resize_panel_window.reset_mock()
        methods = {name for name in dir(PanelAPI) if not name.startswith("_")}
        self.assertEqual(methods, {"panel_ready", "panel_state", "resize_panel", "dismiss", "open_main", "exit_app"})
        for values in ((True, 200), (400, float("nan")), ("400", 200)):
            self.assertFalse(self.panel.resize_panel(*values))
        self.assertTrue(self.panel.resize_panel(9999, 9999))
        self.resize_panel_window.assert_called_with(self.window, 600, 800)
        self.panel.resize_panel(9999, 9999)
        self.assertEqual(self.resize_panel_window.call_count, 1)

    def test_missing_panel_host_keeps_the_native_menu_as_a_fallback(self):
        tray = NativeTray(Mock(), Mock(), show_panel=Mock(side_effect=OSError("launch failed")))
        tray.user, tray.hwnd = Mock(), 123
        tray._launch_panel()
        tray.user.PostMessageW.assert_called_once_with(123, NATIVE_MENU_MESSAGE, 0, 0)
        self.assertEqual(popup_position((1910, 1050), (0, 0, 1920, 1080), (344, 264)), (1566, 786))
        self.assertEqual(popup_position((-1910, 20), (-1920, 0, 0, 1040), (344, 264)), (-1910, 20))


if __name__ == "__main__":
    unittest.main()
