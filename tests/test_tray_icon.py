"""Tray icons follow taskbar DPI and retain a live handle during replacement."""
import ctypes
import unittest
from unittest.mock import Mock

from packet_inspector.tray import NativeTray


class TrayIconTests(unittest.TestCase):
    def setUp(self):
        class Icon(ctypes.Structure):
            _fields_ = [("hIcon", ctypes.c_void_p)]

        self.tray = NativeTray(Mock(), Mock())
        self.tray.user, self.tray.shell = Mock(), Mock()
        self.tray.hwnd, self.tray.icon = 7, Icon(100)
        self.tray.owned_icon, self.tray._icon_size = 100, (16, 16)
        self.tray.user.FindWindowW.return_value = 8
        self.tray.user.GetDpiForWindow.return_value = 144
        self.tray.user.GetSystemMetricsForDpi.side_effect = lambda index, dpi: 16 * dpi // 96
        self.tray.user.GetSystemMetrics.return_value = 16
        self.tray.user.LoadImageW.return_value = 200
        self.tray.shell.Shell_NotifyIconW.return_value = 1

    def test_uses_taskbar_dpi_instead_of_process_system_metrics(self):
        for dpi, size in ((96, 16), (120, 20), (144, 24), (168, 28), (192, 32), (288, 48)):
            with self.subTest(dpi=dpi):
                self.tray.user.GetDpiForWindow.return_value = dpi
                self.assertEqual(self.tray._icon_dimensions(), (size, size))
                self.tray.user.GetDpiForWindow.assert_called_with(8)
        self.tray.user.GetSystemMetrics.assert_not_called()

    def test_missing_dpi_api_falls_back_to_system_metrics(self):
        self.tray.user.GetDpiForWindow.side_effect = AttributeError
        self.assertEqual(self.tray._icon_dimensions(), (16, 16))

    def test_prefers_scale_down_loader_over_legacy_upscaling(self):
        def load(instance, path, width, height, handle):
            handle._obj.value = 300
            return 0
        self.tray._scaled_icon_loader = Mock(side_effect=load)
        self.assertEqual(self.tray._load_icon((28, 28)), 300)
        self.tray.user.LoadImageW.assert_not_called()

    def test_failed_scale_down_loader_uses_legacy_loader(self):
        self.tray._scaled_icon_loader = Mock(return_value=-1)
        self.assertEqual(self.tray._load_icon((24, 24)), 200)
        self.tray.user.LoadImageW.assert_called_once()

    def test_releases_old_icon_only_after_shell_accepts_replacement(self):
        def notify(operation, icon):
            self.assertEqual(operation, 1)
            self.assertEqual(self.tray.icon.hIcon, 200)
            self.tray.user.DestroyIcon.assert_not_called()
            return 1
        self.tray.shell.Shell_NotifyIconW.side_effect = notify
        self.assertTrue(self.tray._refresh_icon())
        self.assertEqual((self.tray.owned_icon, self.tray._icon_size), (200, (24, 24)))
        self.tray.user.DestroyIcon.assert_called_once_with(100)

    def test_failed_shell_update_keeps_previous_icon_and_allows_retry(self):
        self.tray.shell.Shell_NotifyIconW.return_value = 0
        self.assertFalse(self.tray._refresh_icon())
        self.assertEqual((self.tray.icon.hIcon, self.tray.owned_icon, self.tray._icon_size),
                         (100, 100, (16, 16)))
        self.tray.user.DestroyIcon.assert_called_once_with(200)

    def test_failed_load_keeps_existing_product_icon(self):
        self.tray.user.LoadImageW.return_value = None
        self.assertFalse(self.tray._refresh_icon())
        self.assertEqual((self.tray.icon.hIcon, self.tray.owned_icon), (100, 100))
        self.tray.shell.Shell_NotifyIconW.assert_not_called()
        self.tray.user.DestroyIcon.assert_not_called()

    def test_unchanged_size_does_not_reload_icon(self):
        self.tray.user.GetDpiForWindow.return_value = 96
        self.assertTrue(self.tray._refresh_icon())
        self.tray.user.LoadImageW.assert_not_called()
        self.tray.shell.Shell_NotifyIconW.assert_not_called()

    def test_explorer_restart_readds_icon_even_at_unchanged_size(self):
        self.tray.user.GetDpiForWindow.return_value = 96
        self.assertTrue(self.tray._refresh_icon(add=True))
        self.assertEqual(self.tray.shell.Shell_NotifyIconW.call_args.args[0], 0)
        self.tray.user.DestroyIcon.assert_called_once_with(100)


if __name__ == "__main__":
    unittest.main()
