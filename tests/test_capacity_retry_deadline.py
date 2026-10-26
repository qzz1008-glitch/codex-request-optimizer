"""Retry timing against simulated clocks and a real, deliberately stalled HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from packet_inspector import capacity_retry as retry
from tests.test_capacity_retry import CREATED, FAILED, SUCCESS, Opener, Response, frame


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, delay, _):
        self.waits.append(delay)
        self.now += delay


class TimedOpener(Opener):
    def __init__(self, clock, durations, *responses):
        super().__init__(*responses)
        self.clock, self.durations, self.timeouts = clock, list(durations), []

    def open(self, request, timeout):
        self.timeouts.append(timeout)
        self.clock.now += self.durations.pop(0)
        return super().open(request, timeout)


class RetryDeadlineTests(unittest.TestCase):
    def run_timed(self, limit, durations, *, clock=None, deadline=None, wait=None, last=FAILED):
        clock = clock or Clock()
        events = []
        responses = [Response(FAILED) for _ in durations[:-1]] + [Response(last)]
        opener = TimedOpener(clock, durations, *responses)
        with patch.object(retry.time, "monotonic", clock):
            result = retry.open_response(opener, None, enabled=True, max_retries=limit,
                                         deadline=deadline, wait=wait or clock.wait,
                                         on_event=lambda event, **fields: events.append((event, fields)))
            with result:
                body = result.read()
        return body, opener, clock, events

    def test_all_limits_allow_full_count_with_fast_capacity_responses(self):
        for limit in range(1, 10):
            with self.subTest(limit=limit):
                body, opener, clock, _ = self.run_timed(limit, [1] * (limit + 1))
                self.assertEqual(body, FAILED)
                self.assertEqual(len(opener.requests), limit + 1)
                self.assertEqual(clock.waits, [50] * limit)
                self.assertLess(clock.now, 570)

    def test_network_time_comes_out_of_the_shared_budget(self):
        body, opener, clock, _ = self.run_timed(9, [20] + [5] * 9, last=SUCCESS)
        self.assertEqual(body, SUCCESS)
        self.assertEqual(clock.now, 515)
        self.assertEqual(opener.timeouts, [600] + [500 - 55 * n for n in range(9)])

    def test_budget_includes_preparation_before_open_response(self):
        clock = Clock()
        clock.now = 400
        body, opener, clock, events = self.run_timed(9, [5] * 10, clock=clock, deadline=570)
        self.assertEqual(body, FAILED)
        self.assertEqual((len(opener.requests), clock.now), (3, 515))
        self.assertEqual(events[0][1]["remaining_ms"], 170000)
        self.assertEqual(events[-1][1]["reason"], "time_budget")
        self.assertEqual(events[-1][1]["phase"], "before_wait")

    def test_late_original_failure_does_not_schedule_extra_wait(self):
        body, opener, clock, events = self.run_timed(9, [520] + [0] * 9)
        self.assertEqual((body, len(opener.requests), clock.waits), (FAILED, 1, []))
        self.assertEqual(events[-1][1]["reason"], "time_budget")

    def test_scheduler_delay_cannot_start_a_retry_after_deadline(self):
        clock = Clock()
        def oversleep(*_):
            clock.now = 571
        body, opener, _, events = self.run_timed(9, [0] * 10, clock=clock, wait=oversleep)
        self.assertEqual((body, len(opener.requests)), (FAILED, 1))
        self.assertEqual(events[-1][1]["phase"], "after_wait")

    def test_original_generation_does_not_inherit_retry_deadline(self):
        body, opener, _, _ = self.run_timed(9, [590], last=SUCCESS)
        self.assertEqual((body, opener.timeouts), (SUCCESS, [600]))

    def test_socket_timeout_before_clock_deadline_returns_known_capacity_error(self):
        for error in [TimeoutError("timed out"), URLError(TimeoutError("timed out"))]:
            with self.subTest(error=type(error).__name__):
                events = []
                opener = Opener(Response(FAILED), error)
                with retry.open_response(opener, None, enabled=True, wait=lambda *_: None,
                                         on_event=lambda event, **fields: events.append((event, fields))) as result:
                    self.assertEqual(result.read(), FAILED)
                self.assertEqual(events[-1][1]["reason"], "time_budget")
                self.assertEqual(events[-1][1]["error_type"], type(error).__name__)

class StalledRetryTests(unittest.TestCase):
    def test_successful_retry_keeps_streaming_after_retry_budget_expires(self):
        release = threading.Event()
        first, last = frame("response.output_text.delta", delta="Started"), frame("response.completed")
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(first)
                    self.wfile.flush()
                    release.wait(2)
                    self.wfile.write(last)
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        real, calls = build_opener(ProxyHandler({})), []
        class TestOpener:
            def open(self, request, timeout):
                calls.append(timeout)
                return Response(FAILED) if len(calls) == 1 else real.open(request, timeout=timeout)
        timer = threading.Timer(.6, release.set)
        timer.daemon = True
        request = Request(f"http://127.0.0.1:{server.server_port}/responses", data=b"{}")
        try:
            with patch.object(retry, "MAX_RETRY_SECONDS", .3), \
                    patch.object(retry, "MIN_ATTEMPT_SECONDS", 0), \
                    patch.object(retry, "retry_delay", return_value=0):
                with retry.open_response(TestOpener(), request, enabled=True) as result:
                    self.assertEqual(result.read1(), first)
                    timer.start()
                    self.assertEqual(result.read(), last)
        finally:
            release.set()
            timer.cancel()
            server.shutdown()
            server.server_close()
            serving.join(2)

    def exercise_stall(self, mode, stop="deadline"):
        started, release = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                started.set()
                try:
                    if mode == "headers":
                        release.wait(2)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if mode == "prefix":
                        self.wfile.write(CREATED)
                        self.wfile.flush()
                        release.wait(2)
                    self.wfile.write(SUCCESS)
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        real = build_opener(ProxyHandler({}))
        failed, calls, events, workers = Response(FAILED, status=429), [], [], []
        class TestOpener:
            def open(self, request, timeout):
                calls.append(timeout)
                return failed if len(calls) == 1 else real.open(request, timeout=timeout)
        attempt_type = retry._RetryAttempt
        def attempt(*args):
            worker = attempt_type(*args)
            workers.append(worker)
            return worker
        request = Request(f"http://127.0.0.1:{server.server_port}/responses", data=b"{}")
        began = time.monotonic()
        try:
            with patch.object(retry, "MAX_RETRY_SECONDS", .3), \
                    patch.object(retry, "MIN_ATTEMPT_SECONDS", 0), \
                    patch.object(retry, "retry_delay", return_value=0), \
                    patch.object(retry, "_RetryAttempt", side_effect=attempt):
                options = {"cancelled": lambda: stop == "cancel" and started.is_set(),
                           "still_enabled": lambda: stop != "disable" or not started.is_set()}
                if stop == "cancel":
                    with self.assertRaises(BrokenPipeError):
                        retry.open_response(TestOpener(), request, enabled=True,
                                            on_event=lambda event, **fields: events.append((event, fields)), **options)
                    self.assertTrue(failed.closed)
                else:
                    with retry.open_response(TestOpener(), request, enabled=True,
                                             on_event=lambda event, **fields: events.append((event, fields)), **options) as result:
                        self.assertEqual((result.status, result.read()), (429, FAILED))
            self.assertLess(time.monotonic() - began, 1)
            self.assertTrue(started.is_set())
            self.assertEqual(len(calls), 2)
            self.assertLessEqual(calls[1], .301)
            snapshot = list(events)
        finally:
            release.set()
            for worker in workers:
                worker.thread.join(2)
                self.assertFalse(worker.thread.is_alive())
            server.shutdown()
            server.server_close()
            serving.join(2)
        self.assertEqual(events, snapshot, "An abandoned attempt must not update finished diagnostics")
        if stop != "cancel":
            self.assertEqual(events[-1][1]["reason"], "time_budget" if stop == "deadline" else "disabled_during_attempt")
            self.assertEqual(events[-1][1]["status"], 429)

    def test_blocked_response_headers_return_known_error_before_deadline(self):
        self.exercise_stall("headers")

    def test_blocked_sse_prefix_returns_known_error_before_deadline(self):
        self.exercise_stall("prefix")

    def test_client_disconnect_interrupts_inflight_retry(self):
        self.exercise_stall("prefix", "cancel")

    def test_disabling_interrupts_inflight_retry(self):
        self.exercise_stall("prefix", "disable")


if __name__ == "__main__":
    unittest.main()
