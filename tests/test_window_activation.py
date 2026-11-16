"""Explicit tray actions hand foreground rights to the existing desktop host."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from packet_inspector import window_lifecycle
from packet_inspector.desktop import SingleWindow
from packet_inspector.tray import NativeTray


class WindowActivationSignalTests(unittest.TestCase):
    def test_main_signal_grants_activation_before_notifying_and_always_closes_the_handle(self):
        kernel, user, calls = Mock(), Mock(), Mock()
        kernel.OpenEventW.return_value = 123
        calls.attach_mock(user.AllowSetForegroundWindow, "grant")
        calls.attach_mock(kernel.SetEvent, "signal")
        with patch.object(window_lifecycle.os, "name", "nt"), \
                patch.object(window_lifecycle.ctypes, "WinDLL", side_effect=lambda name, **_: kernel if name == "kernel32" else user):
            self.assertTrue(window_lifecycle.request_main_window(Path.cwd()))
        self.assertEqual([call[0] for call in calls.mock_calls], ["grant", "signal"])
        user.AllowSetForegroundWindow.assert_called_once_with(0xFFFFFFFF)
        kernel.CloseHandle.assert_called_once_with(123)
        self.assertTrue(kernel.OpenEventW.call_args.args[-1].endswith("-show"))

    def test_probes_panel_and_upgrade_exit_never_grant_main_window_activation(self):
        kernel, user = Mock(), Mock()
        kernel.OpenEventW.return_value = 123
        with patch.object(window_lifecycle.os, "name", "nt"), \
                patch.object(window_lifecycle.ctypes, "WinDLL", side_effect=lambda name, **_: kernel if name == "kernel32" else user):
            self.assertTrue(window_lifecycle.tray_panel_host_running(Path.cwd()))
            kernel.SetEvent.assert_not_called()
            self.assertTrue(window_lifecycle.request_tray_panel(Path.cwd()))
            self.assertTrue(window_lifecycle.request_window_exit(Path.cwd()))
        user.AllowSetForegroundWindow.assert_not_called()
        self.assertEqual(kernel.CloseHandle.call_count, 3)

    def test_missing_host_uses_launch_fallback_without_granting_unrelated_processes(self):
        kernel, user = Mock(), Mock()
        kernel.OpenEventW.return_value = 0
        with patch.object(window_lifecycle.os, "name", "nt"), \
                patch.object(window_lifecycle.ctypes, "WinDLL", side_effect=lambda name, **_: kernel if name == "kernel32" else user):
            self.assertFalse(window_lifecycle.request_main_window(Path.cwd()))
        user.AllowSetForegroundWindow.assert_not_called()
        kernel.SetEvent.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_repeated_exe_launch_grants_foreground_rights_before_signaling_existing_ui(self):
        instance = SingleWindow.__new__(SingleWindow)
        instance.kernel, instance.event = Mock(), 123
        calls = Mock()
        calls.attach_mock(instance.kernel.SetEvent, "signal")
        with patch("packet_inspector.desktop.allow_window_activation") as grant:
            calls.attach_mock(grant, "grant")
            instance.signal()
        self.assertEqual([call[0] for call in calls.mock_calls], ["grant", "signal"])


class TrayActivationTests(unittest.TestCase):
    def setUp(self):
        self.tray = NativeTray(Mock(), Mock(), show_panel=Mock(), restart_service=Mock())
        self.tray.user, self.tray.hwnd = Mock(), 123

    def test_double_click_grants_foreground_rights_before_dispatching_to_worker_thread(self):
        calls = Mock()
        calls.attach_mock(self.tray.user.SetForegroundWindow, "owner")
        calls.attach_mock(self.tray.user.AllowSetForegroundWindow, "grant")
        calls.attach_mock(self.tray.open_window, "open")
        with patch("packet_inspector.tray.threading.Thread") as thread:
            thread.return_value.start.side_effect = self.tray.open_window
            self.tray._on_icon_message(0x203)
        self.assertEqual([call[0] for call in calls.mock_calls], ["owner", "grant", "open"])
        thread.assert_called_once_with(target=self.tray.open_window, daemon=True)

    def test_single_left_click_is_ignored_and_right_click_keeps_explorer_active(self):
        with patch("packet_inspector.tray.threading.Thread") as thread:
            self.tray._on_icon_message(0x202)
            thread.assert_not_called()
            self.tray._on_icon_message(0x205)
        self.tray.user.SetForegroundWindow.assert_not_called()
        thread.assert_called_once_with(target=self.tray._launch_panel, daemon=True)
        self.tray.open_window.assert_not_called()

    def test_native_open_exit_and_restart_actions_all_use_the_foreground_dispatch(self):
        for selection, callback in ((1, self.tray.open_window), (2, self.tray.exit_app), (3, self.tray.restart_service)):
            with self.subTest(selection=selection), patch.object(self.tray, "_invoke_window_action", create=True) as invoke:
                self.tray.user.TrackPopupMenu.return_value = selection
                self.tray._show_native_menu()
                invoke.assert_called_once_with(callback)


if __name__ == "__main__":
    unittest.main()
