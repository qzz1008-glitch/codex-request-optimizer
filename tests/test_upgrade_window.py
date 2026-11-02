"""Exercise installer feedback with withdrawn windows and temporary data only."""
import gc
import json
import os
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
        self.window = None
        # Destroying a Tk window leaves Python cycles behind. Collect them on
        # the UI thread before a later test starts any background workers.
        gc.collect()

    def test_running_install_cannot_be_closed(self):
        view = UpgradeWindow(self.window, 6)
        view.update_progress(3, "新版服务已就绪，正在准备切换…")
        view.update_progress(2, "正在启动新版服务并检查运行状态…")
        self.assertEqual(view.completed, 3)
        view.close()
        view.chrome.close_button.invoke()
        self.assertTrue(self.window.winfo_exists())

    def test_failure_keeps_the_error_selectable_and_can_be_closed(self):
        view = UpgradeWindow(self.window, 6, "en")
        view.update_progress(4, "正在更新托盘、快捷方式和启动入口…")
        view.finish(False, "升级包校验失败。")
        self.assertEqual(view.heading.cget("text"), "Update interrupted")
        self.assertEqual(view.completed, 4)
        self.assertEqual(view.detail.get("1.0", "end-1c"), "The update package failed verification.")
        self.assertEqual(str(view.detail.cget("state")), "disabled")
        view.chrome.close_button.invoke()
        self.assertFalse(exists(self.window))

    def test_success_reaches_full_progress_and_can_be_closed(self):
        view = UpgradeWindow(self.window, 6, "zh", "v0.7.9", "v0.8.0")
        view.finish(True)
        self.assertEqual(view.completed, 6)
        self.assertEqual(view.heading.cget("text"), "升级已完成")
        view.chrome.close_button.invoke()
        self.assertFalse(exists(self.window))

    def test_white_caption_minimizes_and_close_hover_matches_desktop(self):
        view = UpgradeWindow(self.window, 6)
        self.assertEqual(view.chrome.frame.cget("bg"), "#ffffff")
        with patch.object(view.chrome, "minimize") as minimize:
            view.chrome.minimize_button.invoke()
        minimize.assert_called_once_with()
        view.finish(True)
        view.chrome.close_button.hover(True)
        self.assertEqual(view.chrome.close_button.cget("bg"), "#c42b1c")
        view.chrome.close_button.hover(False)
        self.assertEqual(view.chrome.close_button.cget("bg"), "#ffffff")

    @unittest.skipUnless(os.name == "nt", "Windows non-client frame")
    def test_native_caption_stays_removed_and_client_size_survives_minimize(self):
        import ctypes
        from ctypes import wintypes
        from packet_inspector.upgrade_chrome import _native
        view = UpgradeWindow(self.window, 6)
        self.window.attributes("-alpha", 0)
        dimensions = view.dimensions()
        view.chrome.fit(*dimensions)
        self.window.deiconify()
        self.window.update()
        user, handle = _native(), view.chrome._handle()
        user.SendMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE must still respect installation in progress.
        self.window.update()
        self.assertTrue(exists(self.window))
        for restore in (False, True):
            if restore:
                view.chrome.minimize_button.invoke()
                self.window.update()
                self.window.deiconify()
                self.window.update()
            self.assertEqual(user.GetWindowLongW(handle, -16) & 0x00C00000, 0)
            self.assertTrue(user.GetWindowLongW(handle, -20) & 0x00040000)
            bounds = wintypes.RECT()
            self.assertTrue(user.GetClientRect(handle, ctypes.byref(bounds)))
            self.assertEqual((bounds.right, bounds.bottom), dimensions)

    def test_bilingual_headings_and_versions_fit_at_common_display_scales(self):
        for scale in (1, 1.5, 2):
            for language in ("zh", "en"):
                with self.subTest(scale=scale, language=language):
                    self.window.tk.call("tk", "scaling", scale * 96 / 72)
                    view = UpgradeWindow(self.window, 6, language, "v0.8.0 · abcdef01", "v0.8.0 · 23456789")
                    self.window.update_idletasks()
                    for success in (False, True):
                        view.finish(success, "fixture")
                        self.window.update_idletasks()
                        self.assertLessEqual(view.heading.winfo_reqwidth(), view.u(480))
                    self.assertLessEqual(view.version_label.master.winfo_reqwidth(), view.u(480))
                    # Height can grow to accommodate the native font rather than clipping controls.
                    self.assertLessEqual(self.window.winfo_reqheight(), view.u(440))
                    for child in self.window.winfo_children():
                        child.destroy()


class UpgradeWindowMetadataTests(unittest.TestCase):
    def test_same_version_displays_distinct_current_and_target_builds(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            identity, current, target = "a" * 32, "1" * 64, "2" * 64
            (root / "runtime.json").write_text(json.dumps({"version": "0.8.0", "worker_id": identity}), encoding="utf8")
            workers = root / "router-workers"
            workers.mkdir()
            (workers / (identity + ".json")).write_text(json.dumps({"id": identity,
                "executable": str(root / "bin" / "versions" / current / "CodexPacketInspector.exe")}), encoding="utf8")
            (root / "manifest.json").write_text(json.dumps({"version": "0.8.0",
                "sha256": {"CodexPacketInspector.exe": target}}), encoding="utf8")
            self.assertEqual(window_metadata(SimpleNamespace(data_dir=root, payload=root)),
                             ("zh", "v0.8.0 · 11111111", "v0.8.0 · 22222222"))

    def test_invalid_build_metadata_is_omitted_from_version_labels(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "runtime.json").write_text(json.dumps({"version": "0.8.0", "worker_id": "../outside"}), encoding="utf8")
            for hashes in ([], {"CodexPacketInspector.exe": "unexpected\ntext"}):
                (root / "manifest.json").write_text(json.dumps({"version": "0.8.0", "sha256": hashes}), encoding="utf8")
                self.assertEqual(window_metadata(SimpleNamespace(data_dir=root, payload=root)),
                                 ("zh", "v0.8.0", "v0.8.0"))

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
        del probe
        gc.collect()
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
                    rendered.append((view.state, view.completed))
                    if not success or not open_after or open_error:
                        view.window.after(1, view.close)
                with patch.object(UpgradeWindow, "show", lambda _view: None), patch.object(UpgradeWindow, "finish", render):
                    self.assertEqual(run_upgrade_window(engine, open_after=open_after), 1 if fail else 0)
                # Production exits the installer process after one window; this
                # test opens several. Release each destroyed Tk interpreter on
                # the main thread before the next worker can trigger cyclic GC.
                gc.collect()
                self.assertEqual(rendered, [("failed", 3)] if fail else [("complete", 6)])
                self.assertEqual(engine._open_window.call_count, int(open_after and not fail))


if __name__ == "__main__":
    unittest.main()
