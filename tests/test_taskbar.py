"""Attention suppression must affect only our window and leave focus untouched."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from packet_inspector.taskbar import FlashWindowInfo, HSHELL_FLASH, TaskbarAttention, WM_NCDESTROY
from tests.test_window_lifecycle import Callbacks


class TaskbarAttentionTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock()
        self.window.events = SimpleNamespace(before_show=Callbacks())
        self.attention = TaskbarAttention(self.window)
        self.attention.hwnd = 1234
        self.attention.shell_message = 50001
        self.attention.user = Mock()
        self.attention.common = Mock()
        self.attention.common.DefSubclassProc.return_value = 42

    def test_install_runs_before_window_is_shown(self):
        self.assertEqual(self.window.events.before_show, [self.attention.install])

    def test_clear_stops_flashing_without_activating_window(self):
        self.attention.clear()
        pointer = self.attention.user.FlashWindowEx.call_args.args[0]
        info = ctypes.cast(pointer, ctypes.POINTER(FlashWindowInfo)).contents
        self.assertEqual(info.cbSize, ctypes.sizeof(FlashWindowInfo))
        self.assertEqual((info.hwnd, info.dwFlags, info.uCount, info.dwTimeout), (1234, 0, 0, 0))
        self.attention.user.SetForegroundWindow.assert_not_called()
        self.window.show.assert_not_called()

    def test_own_flash_is_cleared_and_consumed(self):
        result = self.attention._message(1234, 50001, HSHELL_FLASH, 1234, 1, 0)
        self.assertEqual((result, self.attention.suppressed), (0, 1))
        self.attention.user.FlashWindowEx.assert_called_once()
        self.attention.common.DefSubclassProc.assert_not_called()

    def test_other_windows_and_normal_redraw_are_not_affected(self):
        for message, reason, target in [(50001, HSHELL_FLASH, 9999), (50001, 6, 1234), (10, HSHELL_FLASH, 1234)]:
            with self.subTest(message=message, reason=reason, target=target):
                self.assertEqual(self.attention._message(1234, message, reason, target, 1, 0), 42)
        self.attention.user.FlashWindowEx.assert_not_called()
        self.assertEqual(self.attention.suppressed, 0)

    def test_window_destruction_releases_native_registration(self):
        self.attention._message(1234, WM_NCDESTROY, 0, 0, 1, 0)
        self.attention.user.DeregisterShellHookWindow.assert_called_once_with(1234)
        self.attention.common.RemoveWindowSubclass.assert_called_once_with(1234, self.attention.callback, 1)
        self.assertIsNone(self.attention.hwnd)
        self.attention.clear()
        self.attention.user.FlashWindowEx.assert_not_called()

    def test_repeated_install_does_not_duplicate_subclass(self):
        with patch("packet_inspector.taskbar.ctypes.WinDLL") as library:
            self.attention.install()
        library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
