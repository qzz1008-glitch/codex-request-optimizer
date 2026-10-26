"""Retry audits must survive capture eviction and describe actual attempts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.capacity_retry import MAX_PROBE_BYTES, open_response
from packet_inspector.retry_diagnostics import ResponseObservation, RetryJournal
from packet_inspector.storage import Store
from tests.test_capacity_retry import CREATED, FAILED, Opener, Response, frame

COMPLETED = frame("response.completed", response={"status": "completed", "output": []})
SUCCESS = frame("response.output_text.delta", delta="private assistant output") + COMPLETED


class RetryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = RetryJournal(self.root)

    def trace(self, **options):
        return self.journal.begin(session_id="test-thread", enabled=True, **options)

    def run_response(self, *responses, enabled=True, max_retries=3, wait=lambda *_: None, **options):
        trace = self.journal.begin(session_id="test-thread", enabled=enabled, max_retries=max_retries)
        opener = Opener(*responses)
        response = open_response(opener, None, enabled=enabled, max_retries=max_retries,
                                 wait=wait, on_event=trace.record, **options)
        observation = ResponseObservation(trace, response.headers)
        output = bytearray()
        while chunk := response.read1(79):
            observation.feed(chunk)
            output.extend(chunk)
        observation.finish()
        response.close()
        trace.finish("complete", sent_headers=True, response_bytes=len(output))
        return bytes(output), opener, self.journal.get(trace.document["id"])

    def test_success_after_retry_is_distinct_from_exhausted_capacity(self):
        body, opener, record = self.run_response(Response(CREATED + FAILED), Response(SUCCESS))
        self.assertEqual(body, SUCCESS)
        self.assertEqual((record["attempt_count"], record["retry_count"], record["scheduled_retries"]), (2, 1, 1))
        self.assertEqual(record["outcome"], "success")
        self.assertTrue(record["capacity_error_seen"])
        self.assertFalse(record["final_capacity_error"])
        self.assertEqual(record["terminal_event"], "response.completed")
        body, opener, failed = self.run_response(*(Response(FAILED) for _ in range(4)))
        self.assertEqual(body, FAILED)
        self.assertEqual((failed["retry_count"], failed["attempt_count"]), (3, 4))
        self.assertEqual((failed["outcome"], failed["stop_reason"]), ("capacity_error", "retry_limit"))
        self.assertTrue(failed["final_capacity_error"])
        self.assertEqual([event["attempt"] for event in failed["events"] if event["event"] == "attempt_started"], [1, 2, 3, 4])

    def test_snapshot_of_disabled_setting_is_kept_with_detected_error(self):
        body, opener, record = self.run_response(Response(CREATED + FAILED), enabled=False)
        self.assertEqual(body, CREATED + FAILED)
        self.assertFalse(record["enabled"])
        self.assertEqual((record["retry_count"], record["attempt_count"]), (0, 1))
        self.assertEqual((record["outcome"], record["stop_reason"]), ("capacity_error", "disabled"))

    def test_error_after_empty_reasoning_is_observed_without_replaying(self):
        payload = CREATED + frame("response.output_item.added", item={"type": "reasoning", "summary": []}) + FAILED
        body, opener, record = self.run_response(Response(payload, chunk=7))
        self.assertEqual((body, len(opener.requests)), (payload, 1))
        self.assertEqual(record["probe_reason"], "stream_committed")
        self.assertEqual(record["first_output_event"], "response.output_item.added")
        self.assertEqual(record["outcome"], "capacity_error")
        self.assertEqual(record["retry_count"], 0)

    def test_error_with_partial_output_is_still_a_capacity_error_in_audit(self):
        payload = frame("response.failed", response={"output": [{"type": "message"}],
                         "error": {"code": "server_is_overloaded"}})
        body, opener, record = self.run_response(Response(payload))
        self.assertEqual((body, len(opener.requests)), (payload, 1))
        self.assertEqual(record["outcome"], "capacity_error")

    def test_scheduled_retry_disabled_during_wait_is_not_counted_as_sent(self):
        flag = [True]
        def wait(*_):
            flag[0] = False
        _, opener, record = self.run_response(Response(FAILED), still_enabled=lambda: flag[0], wait=wait)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual((record["scheduled_retries"], record["retry_count"]), (1, 0))
        self.assertEqual(record["stop_reason"], "disabled_during_wait")

    def test_cancelled_wait_keeps_scheduled_and_actual_counts_separate(self):
        trace = self.trace()
        opener = Opener(Response(FAILED))
        def wait(*_):
            raise BrokenPipeError("private details")
        with self.assertRaises(BrokenPipeError):
            open_response(opener, None, enabled=True, wait=wait, on_event=trace.record)
        trace.finish("client_disconnected", error_type="BrokenPipeError")
        record = self.journal.get(trace.document["id"])
        self.assertEqual((record["attempt_count"], record["scheduled_retries"], record["retry_count"]), (1, 1, 0))
        self.assertEqual((record["outcome"], record["stop_reason"]), ("cancelled", "client_disconnected"))
        self.assertNotIn("private details", json.dumps(record))

    def test_retry_after_time_budget_is_logged_without_retry(self):
        for value in ("120", "1e300"):
            with self.subTest(value=value):
                body, opener, record = self.run_response(Response(FAILED, extra={"Retry-After": value}))
                self.assertEqual((body, len(opener.requests)), (FAILED, 1))
                self.assertEqual((record["outcome"], record["stop_reason"]), ("capacity_error", "time_budget"))

    def test_exception_opening_retry_is_a_transport_error_after_one_retry(self):
        trace = self.trace()
        opener = Opener(Response(FAILED), OSError("do not save exception content"))
        with self.assertRaises(OSError):
            open_response(opener, None, enabled=True, wait=lambda *_: None, on_event=trace.record)
        trace.finish("relay_error", error_type="OSError")
        record = self.journal.get(trace.document["id"])
        self.assertEqual((record["outcome"], record["retry_count"], record["attempt_count"]), ("transport_error", 1, 2))
        self.assertNotIn("do not save exception content", json.dumps(record))

    def test_upstream_reset_is_not_misreported_as_client_cancellation(self):
        for phase, outcome in (("upstream_open", "transport_error"), ("upstream_read", "transport_error"),
                               ("downstream_write", "cancelled")):
            with self.subTest(phase=phase):
                trace = self.trace()
                trace.record("attempt_started", attempt=1)
                trace.finish("client_disconnected", error_type="ConnectionResetError", error_phase=phase)
                record = self.journal.get(trace.document["id"])
                self.assertEqual(record["outcome"], outcome)
                self.assertEqual(record["events"][-1]["phase"], phase)

    def test_event_budget_preserves_start_and_final_evidence(self):
        trace = self.trace()
        for _ in range(520):
            trace.record("probe_finished", reason="capacity_error", capacity=True)
        trace.finish("complete")
        record = self.journal.get(trace.document["id"])
        self.assertEqual(len(record["events"]), 512)
        self.assertEqual(record["events"][0]["event"], "request_started")
        self.assertEqual(record["events"][-1]["event"], "request_finished")
        self.assertEqual(record["events_dropped"], 10)
        self.assertEqual(record["outcome"], "capacity_error")

    def test_successful_http_status_without_terminal_is_not_invented_success(self):
        _, _, record = self.run_response(Response(CREATED))
        self.assertEqual(record["outcome"], "unobserved")
        self.assertEqual(record["http_status"], 200)

    def test_non_capacity_error_keeps_error_code(self):
        body = json.dumps({"error": {"code": "invalid_prompt", "message": "private prompt details"}}).encode()
        _, _, record = self.run_response(Response(body, 400, "application/json"))
        self.assertEqual(record["outcome"], "upstream_error")
        self.assertEqual(record["stop_reason"], "unsupported_status")
        self.assertEqual(next(e["error_code"] for e in record["events"] if e["event"] == "terminal"), "invalid_prompt")
        self.assertNotIn("private prompt details", json.dumps(record))

    def test_json_sse_split_crlf_missing_type_and_reordered_keys(self):
        payloads = [
            (json.dumps({"status": "completed", "output": []}).encode(), "application/json"),
            (SUCCESS.replace(b"\n", b"\r\n"), None),
            (b'event: error\ndata: {"code":"server_is_overloaded"}\n\n', None),
            (b'data: {"response":{"output":[{"type":"message"}]},"type":"response.completed"}\n\n', None),
            (frame("response.output_text.delta", delta="hello") +
             b'data: {"error":{"type":"server_error","code":"server_is_overloaded"},"type":"error"}\n\n', None)]
        for body, kind in payloads:
            with self.subTest(kind=kind, body=body[:30]):
                _, _, record = self.run_response(Response(body, content_type=kind, chunk=3), enabled=False)
                self.assertIn(record["outcome"], {"success", "capacity_error"})

    def test_message_quoting_capacity_error_does_not_count_as_failure(self):
        body = frame("response.output_text.delta", delta="Selected model is at capacity. Please try a different model.") + COMPLETED
        _, _, record = self.run_response(Response(body))
        self.assertEqual(record["outcome"], "success")
        self.assertFalse(record["capacity_error_seen"])

    def test_encoded_or_unknown_response_is_explicitly_unobserved(self):
        for response in (Response(FAILED, extra={"Content-Encoding": "gzip"}),
                         Response(b"opaque content", content_type="application/octet-stream")):
            _, _, record = self.run_response(response)
            self.assertEqual(record["outcome"], "unobserved")
            self.assertIn(record["observation_reason"], {"encoded_response", "unsupported_content_type"})

    def test_oversize_frame_does_not_hide_later_capacity_error(self):
        trace = self.trace()
        response = Response(b"")
        observation = ResponseObservation(trace, response.headers)
        prefix = b'data: {"type":"response.output_text.delta","delta":"' + b'x' * (MAX_PROBE_BYTES + 100)
        for start in range(0, len(prefix), 65536):
            observation.feed(prefix[start:start + 65536])
        self.assertLessEqual(len(observation.buffer), MAX_PROBE_BYTES)
        observation.feed(b'"}\n\n' + FAILED)
        observation.finish()
        self.assertTrue(trace.document["final_capacity_error"])
        self.assertTrue(any(e.get("reason") == "frame_limit" for e in trace.document["events"]))

    def test_malformed_event_types_do_not_break_forwarding(self):
        for body, kind in [(b'data: {"type":[]}\n\n' + COMPLETED, "text/event-stream"),
                           (b'{"type":[],"status":[]}', "application/json")]:
            returned, _, record = self.run_response(Response(body, content_type=kind))
            self.assertEqual(returned, body)
            self.assertIn(record["outcome"], {"success", "unobserved"})

    def test_audit_survives_packet_and_summary_eviction_and_process_reopen(self):
        store = Store(self.root, max_count=1)
        packet_id = store.add({"thread_id": "test-thread"}, {"private": "body"})
        trace = self.trace(packet_id=packet_id)
        trace.record("attempt_started", attempt=1)
        trace.record("probe_finished", reason="capacity_error", capacity=True)
        trace.finish("complete")
        store.update(packet_id, state="complete")
        store.add({"thread_id": "other-thread"})
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM packets WHERE id=?", (packet_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM packet_summaries WHERE id=?", (packet_id,)).fetchone()[0], 0)
        reopened = RetryJournal(self.root)
        self.assertEqual(reopened.get(packet_id)["outcome"], "capacity_error")
        self.assertEqual(reopened.list(session_id="test-thread")[0]["id"], packet_id)
        self.assertNotIn("events", reopened.list()[0])

    def test_log_does_not_store_headers_payloads_or_error_messages(self):
        trace = self.trace()
        trace.record("probe_finished", reason="capacity_error", capacity=True,
                     authorization="Bearer private-secret", payload="private prompt", message="private message")
        trace.record("attempt_started", attempt=1)
        trace.record("response_headers", status=200)
        observation = ResponseObservation(trace, Response(b"").headers)
        observation.feed(frame("response.output_text.delta", delta="private output") + COMPLETED)
        observation.finish()
        trace.finish("complete")
        record = json.dumps(self.journal.get(trace.document["id"]))
        for private in ("private-secret", "private prompt", "private message", "private output"):
            self.assertNotIn(private, record)

    def test_diagnostic_write_failure_does_not_change_response_or_retry(self):
        with patch.object(self.journal, "_connect", side_effect=sqlite3.OperationalError("locked")):
            body, opener, _ = self.run_response_without_readback()
        self.assertEqual(body, SUCCESS)
        self.assertEqual(len(opener.requests), 2)
        self.assertGreater(self.journal.errors, 0)

    def test_midstream_observation_failure_cannot_escape_into_forwarding(self):
        trace = self.trace()
        observation = ResponseObservation(trace, Response(b"").headers)
        with patch.object(trace, "record", side_effect=sqlite3.OperationalError("locked")):
            observation.feed(FAILED)
        self.assertTrue(observation.stopped)
        self.assertGreater(self.journal.errors, 0)
        self.assertEqual(observation.buffer, b"")

    def test_temporarily_locked_audit_is_not_reported_as_pruned(self):
        trace = self.trace()
        self.assertTrue(self.journal.available(trace.document["id"]))
        with patch.object(self.journal, "_connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertIsNone(self.journal.available(trace.document["id"]))

    def run_response_without_readback(self):
        trace = self.trace()
        opener = Opener(Response(FAILED), Response(SUCCESS))
        response = open_response(opener, None, enabled=True, wait=lambda *_: None, on_event=trace.record)
        body = response.read()
        response.close()
        trace.finish("complete")
        return body, opener, trace

    def test_retention_budgets_prioritize_incidents_and_preserve_active(self):
        clock = [1000000.0]
        self.journal = RetryJournal(self.root, max_records=3, normal_records=1, clock=lambda: clock[0])
        incident = self.trace()
        incident.record("probe_finished", reason="capacity_error", capacity=True)
        incident.finish("complete")
        for _ in range(3):
            clock[0] += 1
            trace = self.trace()
            trace.record("response_headers", status=200)
            trace.record("terminal", event_type="response.completed")
            trace.finish("complete")
        active = self.trace()
        self.journal.prune(force=True)
        self.assertEqual(len(self.journal.list()), 3)
        self.assertEqual(self.journal.get(incident.document["id"])["outcome"], "capacity_error")
        self.assertEqual(self.journal.get(active.document["id"])["state"], "active")
        clock[0] += 31 * 86400
        self.journal.prune(force=True)
        self.assertEqual(self.journal.list(), [])

    def test_content_budget_and_unfinished_trace_recovery_are_bounded(self):
        clock = [1000000.0]
        self.journal = RetryJournal(self.root, max_bytes=2000, clock=lambda: clock[0])
        trace = self.trace()
        clock[0] += 7 * 3600
        self.journal.prune(force=True)
        record = self.journal.get(trace.document["id"])
        self.assertEqual((record["outcome"], record["stop_reason"]), ("unfinished", "no_final_record"))
        for _ in range(4):
            clock[0] += 1
            trace = self.trace()
            trace.record("probe_finished", reason="capacity_error", capacity=True)
            trace.finish("complete")
        self.journal.prune(force=True)
        with closing(sqlite3.connect(self.journal.path)) as db:
            self.assertLessEqual(db.execute("SELECT coalesce(sum(bytes),0) FROM retry_requests").fetchone()[0], 2000)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for retry diagnostic UI checks")
    def test_frontend_outcomes_download_and_actual_retry_markers(self):
        result = subprocess.run([shutil.which("node"), "tests/retry_diagnostics.cjs"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
