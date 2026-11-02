"""The installer preview never installs or starts the real application."""
from pathlib import Path
import tempfile
import unittest

from tests.test_upgrade_window import exists, hidden_window
from scripts.preview_upgrade_window import CHROME_SOURCE, SOURCE, UpgradePreview


class UpgradePreviewTests(unittest.TestCase):
    def setUp(self):
        self.window = hidden_window()
        self.preview = UpgradePreview(self.window)

    def tearDown(self):
        if exists(self.window):
            self.preview.close()

    def test_preview_can_revisit_states_and_languages_without_installing(self):
        preview = self.preview
        self.assertEqual(preview.view.completed, 3)
        for index in range(9):
            preview.select(index)
            self.assertTrue(exists(self.window))
        self.assertIn("无法打开优化器", preview.view.detail.get("1.0", "end"))
        preview.select(7)
        self.assertEqual(preview.view.state, "failed")
        preview.select(6)
        preview.lang.set("en")
        preview.change_language()
        self.assertEqual(preview.view.heading.cget("text"), "Update complete")
        self.assertTrue(exists(self.window))
        preview.select(0)
        self.assertEqual(preview.view.completed, 0)
        self.assertEqual(preview.view.state, "running")
        # Unlike the installer, X / Escape may close a running simulation.
        preview.view.chrome.close_button.invoke()
        self.assertFalse(exists(self.window))

    def test_demo_stays_open_at_completion(self):
        preview = self.preview
        preview.select(5)
        preview.toggle_play()
        self.window.after_cancel(preview.timer)
        preview.advance()
        self.assertEqual(preview.state, 6)
        self.assertFalse(preview.playing)
        self.assertIsNone(preview.timer)
        self.assertTrue(exists(self.window))

    def test_hot_reload_reads_same_size_edits_and_recovers_from_bad_source(self):
        preview = self.preview
        preview.lang.set("en")
        preview.change_language()
        original = SOURCE.read_bytes()
        with tempfile.TemporaryDirectory() as name:
            preview.source = Path(name) / "upgrade_window.py"
            preview.chrome_source = Path(name) / "upgrade_chrome.py"
            preview.chrome_source.write_bytes(CHROME_SOURCE.read_bytes())
            preview.source.write_bytes(original.replace(b'"#315fd7"', b'"#6655cc"'))
            preview.reload_if_changed()
            self.assertEqual(preview.module.COLORS["accent"], "#6655cc")
            for bad in (b"not valid python !", original + b"\nUpgradeWindow = None\n"):
                previous = preview.module
                preview.source.write_bytes(bad)
                preview.reload_if_changed()
                self.assertIs(preview.module, previous)
                self.assertEqual(preview.view.completed, 3)
                self.assertIn("重载失败", preview.note.cget("text"))
                self.assertTrue(preview.view.heading.winfo_exists())
            preview.source.write_bytes(original)
            preview.reload_if_changed()
            preview.chrome_source.write_bytes(CHROME_SOURCE.read_bytes().replace(b'"#c42b1c"', b'"#b42b1c"'))
            preview.reload_if_changed()
            preview.view.chrome.close_button.hover(True)
            self.assertEqual(preview.view.chrome.close_button.cget("bg"), "#b42b1c")
        self.assertEqual(preview.module.COLORS["accent"], "#315fd7")
        self.assertEqual(preview.language, "en")
        self.assertEqual(preview.view.completed, 3)
        self.assertIn("已重载", preview.note.cget("text"))


if __name__ == "__main__":
    unittest.main()
