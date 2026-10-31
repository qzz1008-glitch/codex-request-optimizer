"""Exercise installer feedback with withdrawn windows and temporary data only."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import tkinter as tk
    from packet_inspector.upgrade_window import UpgradeWindow, run_upgrade_window, window_metadata
except ImportError:
    raise unittest.SkipTest("Tk is not installed")


def hidden_window():
    try:
        window = tk.Tk()
    except tk.TclError as error:
        raise unittest.SkipTest(str(error))
    window.withdraw()
    return window


def exists(window):
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


class UpgradeWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = hidden_window()
        self.addCleanup(self.destroy)

    def destroy(self):
        if exists(self.window):
            self.window.destroy()

    def test_running_install_cannot_be_closed_or_open_another_application(self):
        opener = Mock()
        view = UpgradeWindow(self.window, 6, open_app=opener)
        view.update_progress(3, "新版服务已就绪，正在准备切换…")
        view.update_progress(2, "正在启动新版服务并检查运行状态…")
        self.assertEqual(view.percentage.cget("text"), "50%")
        view.close()
        view.open_application()
        self.assertTrue(self.window.winfo_exists())
        opener.assert_not_called()
        self.assertEqual(str(view.close_button.cget("state")), "disabled")

    def test_failure_keeps_the_error_selectable_and_does_not_offer_to_open(self):
        view = UpgradeWindow(self.window, 6, "en")
        view.update_progress(4, "正在更新托盘、快捷方式和启动入口…")
        view.finish(False, "升级包校验失败。")
        self.assertEqual(view.heading.cget("text"), "Update interrupted")
        self.assertEqual(view.percentage.cget("text"), "67%")
        self.assertEqual(view.detail.get("1.0", "end-1c"), "The update package failed verification.")
        self.assertEqual(str(view.detail.cget("state")), "disabled")
        self.assertEqual(str(view.open_button.cget("state")), "disabled")
        self.assertEqual(str(view.close_button.cget("state")), "normal")

    def test_success_opens_the_application_and_closes_the_installer(self):
        opener = Mock()
        view = UpgradeWindow(self.window, 6, "zh", "v0.7.9", "v0.8.0", opener)
        view.finish(True)
        self.assertEqual(view.percentage.cget("text"), "100%")
        self.assertEqual(view.heading.cget("text"), "升级已完成")
        view.open_application()
        opener.assert_called_once_with()
        self.assertFalse(exists(self.window))

    def test_launch_failure_is_displayed_in_the_same_window(self):
        view = UpgradeWindow(self.window, 6, "en", open_app=Mock(side_effect=OSError("fixture launch failure")))
        view.finish(True)
        view.open_application()
        self.assertTrue(self.window.winfo_exists())
        self.assertEqual(view.detail.get("1.0", "end-1c"), "Could not open the optimizer: fixture launch failure")
        self.assertEqual(view.state, "complete")

    def test_bilingual_headings_and_actions_fit_at_common_display_scales(self):
        for scale in (1, 1.5, 2):
            for language in ("zh", "en"):
                with self.subTest(scale=scale, language=language):
                    self.window.tk.call("tk", "scaling", scale * 96 / 72)
                    view = UpgradeWindow(self.window, 6, language, "v0.7.9", "v0.8.0")
                    self.window.update_idletasks()
                    for success in (False, True):
                        view.finish(success, "fixture")
                        self.window.update_idletasks()
                        self.assertLessEqual(view.heading.winfo_reqwidth(), view.u(480))
                    self.assertLessEqual(view.close_button.master.winfo_reqwidth(), view.u(480))
                    # Height can grow to accommodate the native font rather than clipping controls.
                    self.assertLessEqual(self.window.winfo_reqheight(), view.u(440))
                    for child in self.window.winfo_children():
                        child.destroy()


class UpgradeWindowMetadataTests(unittest.TestCase):
    def test_uses_saved_language_and_both_versions_without_requiring_a_service(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "settings.json").write_text(json.dumps({"ui_language": "en"}), encoding="utf8")
            (root / "runtime.json").write_text(json.dumps({"version": "0.7.9"}), encoding="utf8")
            (root / "manifest.json").write_text(json.dumps({"version": "0.8.0"}), encoding="utf8")
            self.assertEqual(window_metadata(SimpleNamespace(data_dir=root, payload=root)), ("en", "v0.7.9", "v0.8.0"))

    def test_missing_or_invalid_metadata_does_not_hide_an_installation_error(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "settings.json").write_text("[]", encoding="utf8")
            (root / "runtime.json").write_text("{", encoding="utf8")
            (root / "manifest.json").write_text(json.dumps({"version": "x" * 5000}), encoding="utf8")
            self.assertEqual(window_metadata(SimpleNamespace(data_dir=root, payload=root)), ("zh", "", ""))


class UpgradeWindowRunnerTests(unittest.TestCase):
    def test_worker_outcomes_are_rendered_on_the_ui_thread(self):
        # No live service is contacted and these installer windows never become visible.
        probe = hidden_window()
        probe.destroy()
        for fail, open_after, open_error in ((False, False, False), (False, True, False), (True, True, False), (False, True, True)):
            with self.subTest(fail=fail, open_after=open_after, open_error=open_error), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                engine = SimpleNamespace(data_dir=root, payload=root, total_steps=6, completed_steps=0, _open_window=Mock())
                if open_error:
                    engine._open_window.side_effect = OSError("fixture launch failure")
                def work():
                    engine.completed_steps = 3
                    engine.progress("新版服务已就绪，正在准备切换…")
                    if fail:
                        raise RuntimeError("fixture upgrade failure")
                    engine.completed_steps = 6
                    engine.progress("升级完成。旧服务会在请求结束后退出。")
                engine.run = work
                finish = UpgradeWindow.finish
                rendered = []
                def render(view, success, detail):
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    finish(view, success, detail)
                    rendered.append((view.state, view.percentage.cget("text")))
                    if not success or not open_after or open_error:
                        view.window.after(1, view.close)
                with patch.object(UpgradeWindow, "show", lambda _view: None), patch.object(UpgradeWindow, "finish", render):
                    self.assertEqual(run_upgrade_window(engine, open_after=open_after), 1 if fail else 0)
                self.assertEqual(rendered, [("failed", "50%")] if fail else [("complete", "100%")])
                self.assertEqual(engine._open_window.call_count, int(open_after and not fail))


if __name__ == "__main__":
    unittest.main()
