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
from packet_inspector.sharing_upgrade import CANCELLED_EXIT, UpgradeCancelled

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
    def test_confirmation_requires_an_explicit_choice_and_close_means_not_now(self):
        view = UpgradeWindow(self.window, 9)
        decision = Mock()
        with patch.object(view, "show"):
            view.request_confirmation(decision)
            self.assertEqual(view.state, "confirming")
            decision.assert_not_called()
            self.assertEqual(view.cancel_button.cget("text"), "暂不升级")
            self.assertEqual(view.confirm_button.cget("text"), "暂停服务并升级")
            view.close()
            self.assertEqual(view.state, "running")
            self.assertTrue(exists(self.window))
            decision.assert_called_once_with(False)
            view.decide_confirmation(True)
            self.assertEqual(decision.call_count, 1)

    def test_worker_waits_for_main_thread_confirmation_and_does_not_open_app_on_cancel(self):
        self.window.destroy()  # This runner owns its own Tk root and main loop.
        for approved in (False, True):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                engine = SimpleNamespace(data_dir=root, payload=root, total_steps=9, completed_steps=0, _open_window=Mock())
                mutations = []
                def work():
                    if not engine.confirm_interruption({"roles": ["quota_out"]}):
                        raise UpgradeCancelled("已暂不升级，现有服务保持运行。")
                    mutations.append("pause-and-update")
                    engine.completed_steps = 9
                engine.run = work
                request, finish, cancelled = UpgradeWindow.request_confirmation, UpgradeWindow.finish, UpgradeWindow.cancelled
                def show_question(view, callback):
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    self.assertEqual(mutations, [])
                    request(view, callback)
                    view.window.after(1, lambda: view.decide_confirmation(approved))
                def done(view, success, detail):
                    finish(view, success, detail)
                    view.window.after(1, view.close)
                def postpone(view, detail):
                    self.assertIs(threading.current_thread(), threading.main_thread())
                    cancelled(view, detail)
                    self.assertEqual(view.heading.cget("text"), "已暂不升级")
                    view.window.after(1, view.close)
                with patch.object(UpgradeWindow, "show", lambda _view: None), \
                     patch.object(UpgradeWindow, "request_confirmation", show_question), \
                     patch.object(UpgradeWindow, "finish", done), \
                     patch.object(UpgradeWindow, "cancelled", postpone):
                    self.assertEqual(run_upgrade_window(engine, open_after=True), 0 if approved else CANCELLED_EXIT)
                gc.collect()
                self.assertEqual(mutations, ["pause-and-update"] if approved else [])
                self.assertEqual(engine._open_window.call_count, int(approved))

    def test_destroying_pending_confirmation_releases_the_waiting_worker_without_approval(self):
        self.window.destroy()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            completed, approvals = threading.Event(), []
            engine = SimpleNamespace(data_dir=root, payload=root, total_steps=9, completed_steps=0)
            def work():
                approvals.append(engine.confirm_interruption({"roles": ["network_out"]}))
                completed.set()
                raise UpgradeCancelled("已暂不升级，现有服务保持运行。")
            engine.run = work
            request = UpgradeWindow.request_confirmation
            def question(view, callback):
                request(view, callback)
                view.window.after(1, view.window.destroy)
            with patch.object(UpgradeWindow, "show", lambda _view: None), patch.object(UpgradeWindow, "request_confirmation", question):
                run_upgrade_window(engine)
            self.assertTrue(completed.wait(2))
            self.assertEqual(approvals, [False])
            gc.collect()

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

    def test_progress_has_no_milestones_and_only_the_latest_log_line(self):
        view = UpgradeWindow(self.window, 9)
        self.window.update_idletasks()
        view.update_progress(3, "previous log")
        view.update_progress(4, "latest log\nsecond physical line")
        self.assertEqual(view.detail.get("1.0", "end-1c"), "latest log second physical line")
        self.assertEqual(view.detail.cget("wrap"), "none")
        self.assertEqual(view.detail.cget("height"), 1)
        with patch.object(view.bar, "winfo_width", return_value=480):
            view.draw_progress()
        self.assertTrue(view.bar.find_withtag("progress"))
        self.assertFalse(view.bar.find_withtag("stage-label"))
        self.assertFalse(view.bar.find_withtag("milestone"))
        self.assertIsNotNone(view.animation_timer)
        view.finish(True)
        self.assertIsNone(view.animation_timer)

    def test_long_error_remains_complete_selectable_and_on_one_line(self):
        view = UpgradeWindow(self.window, 9)
        message = "failure: " + "diagnostic detail " * 100
        view.finish(False, message)
        self.assertEqual(view.detail.get("1.0", "end-1c"), message)
        view.select_log(None)
        self.assertEqual(view.detail.get("sel.first", "sel.last"), message)

    def test_white_caption_minimizes_and_close_hover_matches_desktop(self):
        view = UpgradeWindow(self.window, 6)
        self.assertEqual(view.chrome.frame.cget("bg"), "#ffffff")
        self.assertEqual(view.chrome.title_label.cget("text"), "Codex请求优化器")
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
                    title = "Codex Request Optimizer" if language == "en" else "Codex请求优化器"
                    self.assertEqual(view.chrome.title_label.cget("text"), title)
                    self.assertLessEqual(view.chrome.title_label.winfo_reqwidth() + view.u(92), view.u(536))
                    with patch.object(view, "show"):
                        view.request_confirmation(Mock())
                        self.window.update_idletasks()
                        self.assertLessEqual(view.heading.winfo_reqwidth() + view.u(12) + view.version_label.master.winfo_reqwidth(), view.u(480))
                        self.assertLessEqual(view.cancel_button.winfo_reqwidth() + view.confirm_button.winfo_reqwidth(), view.u(480))
                        self.assertLessEqual(view.confirm_text.winfo_reqwidth(), view.u(480))
                        self.assertLessEqual(self.window.winfo_reqheight(), view.u(440))
                        view.decide_confirmation(False)
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
