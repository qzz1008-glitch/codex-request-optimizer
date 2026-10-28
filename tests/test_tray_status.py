"""Tray counters use retained metadata and authentic pre-compaction input usage."""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from packet_inspector.storage import Store
from packet_inspector.tray import NativeTray, tray_status_lines


def usage(inputs):
    return {"input_tokens": inputs, "output_tokens": 10, "total_tokens": inputs + 10}


class TrayStatisticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root, max_count=100, max_detail_count=0)

    def record(self, *, compact=False, state="complete", status=200, **fields):
        packet = self.store.add({"method": "POST", "path": "/responses/compact" if compact else "/responses",
                                 "is_compaction": compact, "thread_id": "fixture", **fields})
        self.store.update(packet, state=state, status=status)
        return packet

    def test_only_compact_input_usage_enters_the_average_and_protection_is_counted_once(self):
        self.record(upstream_usage=usage(900000))
        self.record(usage_override_applied=True)
        self.record(overflow_compaction_calls=3, overflow_cache_hit=True, native_retry_advised=True)
        self.record(state="relay_error", status=502, relay_error="overflow_compaction_upstream_error")
        self.record(capacity_retry_count=4)
        self.record(native_retry_advised=True)
        self.record(capacity_retry_diagnostic={"scheduled_retries": 9})
        self.record(compact=True, upstream_usage=usage(200000), reported_usage=usage(500000))
        self.record(compact=True, upstream_usage=usage(400000))
        self.record(compact=True, reported_usage=usage(500000))
        self.record(compact=True, status=500, state="relay_error", upstream_usage=usage(800000))
        self.record(compact=True, upstream_usage={**usage(100), "input_tokens": True})
        with patch.object(self.store, "get", side_effect=AssertionError("loaded request body")), \
                patch("packet_inspector.storage.json.loads", side_effect=AssertionError("decoded metadata")):
            result = self.store.tray_statistics()
        expected = {"recorded_requests": 12, "protected_requests": 5,
                    "compaction_samples": 2, "average_compact_input_tokens": 300000}
        self.assertEqual({key: result[key] for key in expected}, expected)
        reopened = Store(self.root, max_count=100, max_detail_count=0)
        self.assertEqual(reopened.tray_statistics(), result)

    def test_revision_cache_tracks_usage_updates_and_retention_without_writing_data(self):
        packet = self.record(compact=True, upstream_usage=usage(200000))
        connect, queries = self.store.connect, []
        @contextmanager
        def traced():
            with connect() as connection:
                connection.set_trace_callback(queries.append)
                yield connection
        with patch.object(self.store, "connect", traced):
            first = self.store.tray_statistics()
            first["recorded_requests"] = 99
            self.assertEqual(self.store.tray_statistics()["recorded_requests"], 1)
        self.assertEqual(sum(query.startswith("WITH counters") for query in queries), 1)
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.store.update(packet, upstream_usage=usage(400000))
        self.assertEqual(self.store.tray_statistics()["average_compact_input_tokens"], 400000)
        self.record(compact=True, upstream_usage=usage(200000))
        self.assertEqual(self.store.tray_statistics()["average_compact_input_tokens"], 300000)
        self.store.configure_limits(1_000_000, 1, 0)
        self.assertEqual(self.store.tray_statistics()["average_compact_input_tokens"], 200000)
        self.store.configure_limits(1, 1, 0)
        self.assertEqual(self.store.tray_statistics(), {"recorded_requests": 0, "protected_requests": 0,
                                                       "recorded_sessions": 0, "first_recorded_at": None, "last_recorded_at": None,
                                                       "compaction_samples": 0, "average_compact_input_tokens": None})

    def test_record_scope_uses_retained_times_and_distinct_nonempty_session_ids(self):
        for timestamp, thread in ((1000, "one"), (2000, "two"), (3000, "one"), (4000, ""), (5000, " ")):
            with patch("packet_inspector.storage.time.time", return_value=timestamp):
                self.record(thread_id=thread)
        result = self.store.tray_statistics()
        self.assertEqual((result["recorded_requests"], result["recorded_sessions"], result["first_recorded_at"], result["last_recorded_at"]),
                         (5, 2, 1000, 5000))
        self.store.configure_limits(1_000_000, 2, 0)
        result = self.store.tray_statistics()
        self.assertEqual((result["recorded_requests"], result["recorded_sessions"], result["first_recorded_at"], result["last_recorded_at"]),
                         (2, 0, 4000, 5000))

    def test_missing_malformed_and_non_compact_usage_never_becomes_a_zero_sample(self):
        for value in ({"input_tokens": 500000}, usage(-1), {**usage(123), "input_tokens": 123.5}):
            self.record(compact=True, upstream_usage=value)
        self.record(compact=True, method="GET", upstream_usage=usage(500000))
        self.record(upstream_usage=usage(600000))
        self.record(capacity_retry_count="0", overflow_compaction_calls="unknown", usage_override_applied=1)
        result = self.store.tray_statistics()
        self.assertEqual(result["protected_requests"], 0)
        self.assertEqual(result["compaction_samples"], 0)
        self.assertIsNone(result["average_compact_input_tokens"])


class TrayMenuTests(unittest.TestCase):
    def snapshot(self, **fields):
        return {"connection_state": "connected", "relay_state": "running", "language": "zh",
                "recorded_requests": 1234, "protected_requests": 12,
                "compaction_samples": 2, "average_compact_input_tokens": 234567, **fields}

    def test_requested_layout_has_three_readonly_counters_and_only_open_and_exit_actions(self):
        tray = NativeTray(Mock(), Mock())
        tray.update_status(self.snapshot())
        items = tray.menu_items()
        self.assertEqual([item[1] for item in items], ["已接入 · 中转正常", None,
            "记录请求：1,234 个", "触发保护：12 次", "平均上下文窗口：234,567 Token", None,
            "打开主窗口", None, "退出程序"])
        self.assertEqual([identity for identity, text, flags in items if not flags], [1, 2])
        self.assertTrue(all(flags == 0x3 for identity, text, flags in items if identity >= 100))

    def test_language_connection_and_missing_values_are_independent_of_the_average(self):
        lines = tray_status_lines(self.snapshot(language="en", connection_state="disconnected", compaction_samples=0))
        self.assertEqual(lines[0], "Disconnected · Relay running")
        self.assertEqual(lines[3], "Average context window: — Token")
        self.assertEqual(tray_status_lines({})[1:], ("记录请求：— 个", "触发保护：— 次", "平均上下文窗口：— Token"))
        self.assertEqual(tray_status_lines(self.snapshot(average_compact_input_tokens=0))[3], "平均上下文窗口：0 Token")

    def test_open_native_menu_updates_only_changed_rows_and_preserves_actions(self):
        class Icon(ctypes.Structure):
            _fields_ = [("szTip", ctypes.c_wchar * 128)]
        tray = NativeTray(Mock(), Mock())
        tray.update_status(self.snapshot())
        tray._menu_text = {identity: text for identity, text, flags in tray.menu_items() if identity}
        tray.user, tray.shell, tray.icon = Mock(), Mock(), Icon()
        tray.hwnd, tray._menu = 123, 456
        tray.update_status(self.snapshot(recorded_requests=1235, protected_requests=13))
        tray._refresh_menu()
        self.assertEqual([call.args[1] for call in tray.user.ModifyMenuW.call_args_list], [101, 102])
        self.assertTrue(all(call.args[2] == 0x3 for call in tray.user.ModifyMenuW.call_args_list))
        tray._refresh_menu()
        self.assertEqual(tray.user.ModifyMenuW.call_count, 2)
        tray.open_window.assert_not_called()
        tray.exit_app.assert_not_called()

    def test_reader_stays_idle_when_closed_and_failure_does_not_show_stale_counts(self):
        reader = Mock(return_value=self.snapshot())
        tray = NativeTray(Mock(), Mock(), status_reader=reader)
        tray.status_thread = threading.Thread(target=tray._status_loop, daemon=True)
        self.addCleanup(tray.stop)
        tray.status_thread.start()
        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(predicate())
        tray._status_requested.set()
        wait_for(lambda: "1,234" in tray.menu_items()[2][1])
        time.sleep(1.05)
        self.assertEqual(reader.call_count, 1)
        reader.side_effect = OSError("unavailable")
        tray._menu_open = True
        tray._status_requested.set()
        wait_for(lambda: tray.menu_items()[0][1] == "状态暂不可用")
        self.assertIn("—", tray.menu_items()[2][1])
        tray.stop()
        self.assertFalse(tray.status_thread.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows popup menu integration")
    def test_native_menu_strings_update_through_the_window_message_queue(self):
        opened, exited = Mock(), Mock()
        tray = NativeTray(opened, exited).start()
        self.addCleanup(tray.stop)
        user = tray.user
        user.GetMenuStringW.argtypes = [ctypes.wintypes.HMENU, ctypes.wintypes.UINT,
                                       ctypes.wintypes.LPWSTR, ctypes.c_int, ctypes.wintypes.UINT]
        user.GetMenuState.argtypes = [ctypes.wintypes.HMENU, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        menu = user.CreatePopupMenu()
        try:
            for identity, text, flags in tray.menu_items():
                self.assertTrue(user.AppendMenuW(menu, flags, identity, text))
                if identity:
                    tray._menu_text[identity] = text
            tray._menu = menu
            tray.update_status(self.snapshot())
            def text(identity):
                buffer = ctypes.create_unicode_buffer(256)
                user.GetMenuStringW(menu, identity, buffer, len(buffer), 0)
                return buffer.value
            deadline = time.monotonic() + 2
            while "1,234" not in text(101) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(text(100), "已接入 · 中转正常")
            self.assertEqual(text(101), "记录请求：1,234 个")
            self.assertEqual(text(103), "平均上下文窗口：234,567 Token")
            self.assertEqual(user.GetMenuState(menu, 101, 0) & 3, 3)
            self.assertEqual(user.GetMenuState(menu, 1, 0) & 3, 0)
            self.assertEqual(text(1), "打开主窗口")
            self.assertEqual(text(2), "退出程序")
            opened.assert_not_called()
            exited.assert_not_called()
        finally:
            tray._menu = None
            user.DestroyMenu(menu)


if __name__ == "__main__":
    unittest.main()
