"""Verify usage triggering without native patches, history writes or real models."""
from copy import deepcopy
from email.message import Message
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".local" / "builddeps"))
from packet_inspector.analyzer import decode_body
from packet_inspector.server import InspectorServer
from packet_inspector.token_window import TokenWindows
from packet_inspector.transport import prepare_request
from packet_inspector.usage_trigger import UsageStream, request_size, TRIGGER_BYTES

USAGE = {"input_tokens": 123456, "output_tokens": 73, "total_tokens": 123529,
         "input_tokens_details": {"cached_tokens": 120000}, "future_usage": 17}
DELTA = b'data: {"type":"response.output_text.delta","delta":"unchanged"}\n\n'


def completed(usage=None):
    return {"type": "response.completed", "response": {"status": "completed", "id": "test-response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "unchanged"}]}],
            "usage": deepcopy(USAGE if usage is None else usage)}}


def sse(value, newline=b"\n"):
    return b"data: " + json.dumps(value).encode() + newline + newline


def request_body(compressible=False, kind="turn", text_bytes=1_850_000):
    text = "x" * text_bytes if compressible else hashlib.shake_256(b"native-trigger").hexdigest(text_bytes // 2)
    return json.dumps({"model": "test", "stream": True, "input": [{"role": "user", "content": text}],
                       "client_metadata": {"x-codex-turn-metadata": json.dumps({"request_kind": kind})}}).encode()


class Response:
    def __init__(self, body, mode="text/event-stream", status=200, chunk=65536, encoding=None):
        self.body, self.chunk, self.status = io.BytesIO(body), chunk, status
        self.headers = Message()
        if mode is not None:
            self.headers["Content-Type"] = mode
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self, count=-1):
        return self.body.read(min(count, self.chunk) if count >= 0 else self.chunk)

    read1 = read


class UsageStreamTests(unittest.TestCase):
    def test_split_bom_and_json_media_types_work_without_version_specific_handling(self):
        bom = b"\xef\xbb\xbf"
        for rewrite in (False, True):
            for mode, payload in ((None, bom + sse(completed())),
                                  (None, bom + json.dumps(completed()["response"]).encode()),
                                  ("application/response+json", json.dumps(completed()["response"]).encode())):
                with self.subTest(rewrite=rewrite, mode=mode):
                    records = []
                    stream = UsageStream(Response(payload, mode=mode, chunk=1), lambda **v: records.append(v), rewrite=rewrite)
                    output = b"".join(stream.chunks())
                    if not rewrite:
                        self.assertEqual(output, payload)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["upstream_usage"], USAGE)

    def test_observation_preserves_every_chunk_and_does_not_delay_json_or_sse(self):
        for mode, payload in (("text/event-stream", DELTA + sse(completed())),
                              ("application/json", json.dumps(completed()["response"]).encode()),
                              (None, DELTA + sse(completed()))):
            with self.subTest(mode=mode):
                records = []
                response = Response(payload, mode=mode, chunk=7)
                stream = UsageStream(response, lambda **v: records.append(v), rewrite=False)
                chunks = stream.chunks()
                self.assertEqual(next(chunks), payload[:7])
                self.assertEqual(response.body.tell(), 7)
                self.assertEqual(payload[:7] + b"".join(chunks), payload)
                self.assertEqual(records, [{"upstream_usage": USAGE}])

    def test_observation_recovers_after_oversized_frames_and_ignores_unknown_events(self):
        payload = sse({"type": "future.event", "value": "x" * 5000}) + sse(completed())
        records = []
        with patch("packet_inspector.usage_trigger.MAX_EVENT_BYTES", 1000):
            stream = UsageStream(Response(payload, chunk=71), lambda **v: records.append(v), rewrite=False)
            self.assertEqual(b"".join(stream.chunks()), payload)
        self.assertEqual(records, [{"upstream_usage": USAGE}])

    def transform(self, body, **fields):
        records = []
        stream = UsageStream(Response(body, **fields), lambda **v: records.append(v))
        return b"".join(stream.chunks()), records

    def test_sse_changes_only_terminal_usage_across_read_and_crlf_boundaries(self):
        for chunk in (1, 2, 3, 7, 65536):
            for newline in (b"\n", b"\r\n"):
                terminal = b"id: keep" + newline + sse(completed(), newline)
                output, records = self.transform(DELTA + terminal, chunk=chunk)
                self.assertTrue(output.startswith(DELTA + b"id: keep" + newline))
                data = output[len(DELTA):].split(b"data: ", 1)[1].strip()
                result = json.loads(data)
                expected = completed()
                expected["response"]["usage"].update(input_tokens=500000, total_tokens=500073)
                self.assertEqual(result, expected)
                self.assertEqual(records[0]["upstream_usage"], USAGE)
                self.assertTrue(records[0]["usage_override_applied"])

    def test_json_preserves_outputs_and_records_original(self):
        value = completed()["response"]
        output, records = self.transform(json.dumps(value).encode(), mode="application/json")
        result = json.loads(output)
        self.assertEqual(result["output"], value["output"])
        self.assertEqual(result["usage"]["input_tokens"], 500000)
        self.assertEqual(records[0]["upstream_usage"], USAGE)

    def test_missing_content_type_still_rewrites_native_sse_and_json(self):
        for chunk in (1, 7, 65536):
            output, records = self.transform(DELTA + sse(completed()), mode=None, chunk=chunk)
            self.assertTrue(output.startswith(DELTA))
            result = json.loads(output[len(DELTA):].split(b"data: ", 1)[1].strip())
            self.assertEqual(result["response"]["usage"]["input_tokens"], 500000)
            self.assertEqual(records[0]["upstream_usage"], USAGE)
            output, records = self.transform(json.dumps(completed()["response"]).encode(), mode=None, chunk=chunk)
            self.assertEqual(json.loads(output)["usage"]["input_tokens"], 500000)
            self.assertEqual(records[0]["upstream_usage"], USAGE)

    def test_invalid_missing_or_failed_usage_is_not_invented(self):
        for usage in ({}, {"input_tokens": True, "output_tokens": 2, "total_tokens": 3}):
            raw = sse(completed(usage))
            self.assertEqual(self.transform(raw), (raw, []))
        value = completed(); value["response"]["status"] = "incomplete"
        raw = sse(value)
        self.assertEqual(self.transform(raw), (raw, []))
        for raw in (b"data: invalid\n\n", b"data: [DONE]\n\n", b": heartbeat\n\n"):
            self.assertEqual(self.transform(raw), (raw, []))

    def test_large_events_pass_through_without_disabling_later_completion(self):
        large = sse({"type": "response.output_text.delta", "delta": "x" * 4096})
        with patch("packet_inspector.usage_trigger.MAX_EVENT_BYTES", 768):
            result, records = self.transform(large + sse(completed()), chunk=101)
        self.assertTrue(result.startswith(large))
        self.assertTrue(records[0]["usage_override_applied"])

    def test_high_usage_is_never_lowered(self):
        usage = {**USAGE, "input_tokens": 600000, "total_tokens": 600073}
        raw = sse(completed(usage))
        result, records = self.transform(raw)
        self.assertEqual(result, raw)
        self.assertFalse(records[0]["usage_override_applied"])

    def test_encoded_or_error_responses_remain_byte_exact(self):
        raw = sse(completed())
        for fields in ({"encoding": "gzip"}, {"status": 400}, {"mode": "text/plain"}):
            self.assertEqual(self.transform(raw, **fields), (raw, []))

    def test_threshold_uses_encoded_wire_size_and_excludes_native_compaction(self):
        body = request_body(compressible=True)
        wire, _ = prepare_request(body, {}, "POST", "/responses", "zstd")
        self.assertGreater(len(body), 1000000)
        self.assertLess(len(wire), TRIGGER_BYTES)
        self.assertIsNone(request_size(body, {}, "POST", "/responses", len(wire)))
        self.assertEqual(request_size(body, {}, "POST", "/responses", 950000), 950000)
        self.assertEqual(request_size(body, {}, "POST", "/responses", 1000000), 1000000)
        self.assertIsNone(request_size(body, {}, "POST", "/responses", 949999))
        self.assertIsNone(request_size(request_body(kind="compaction"), {}, "POST", "/responses", 1000000))
        self.assertIsNone(request_size(body, {}, "POST", "/responses/compact", 1000000))

    def test_missing_original_usage_never_displays_client_override(self):
        reader = TokenWindows()
        packet = {"usage_override_applied": True, "upstream_usage": {"input_tokens": "invalid"}}
        with patch.object(reader, "lookup", return_value={"state": "available", "input_tokens": 500000}):
            self.assertEqual(reader.lookup_ready(packet), {"state": "unavailable"})


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((decode_body(raw, self.headers.get("Content-Encoding", "identity")),
                                     self.headers.get("Accept-Encoding")))
        output = DELTA + sse(completed())
        self.send_response(200)
        if self.headers.get("X-Test-Missing-Type") != "1":
            self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(output)))
        self.send_header("ETag", '"old-body"')
        self.end_headers()
        self.wfile.write(DELTA); self.wfile.flush()
        if self.headers.get("X-Test-Hold") == "1":
            self.server.release.wait(10)
        self.wfile.write(output[len(DELTA):]); self.wfile.flush()


class UsageRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root / "config.toml"; config.write_text('model="test"', encoding="utf8")
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.up.requests, self.up.release = [], threading.Event()
        self.server = InspectorServer(("127.0.0.1", 0), self.root / "data", config,
                                      "http://127.0.0.1:" + str(self.up.server_port))
        self.server.settings["packet_size_protection_enabled"] = True
        self.original_config = config.read_bytes()
        for server in (self.server, self.up):
            # Join handlers before deleting SQLite fixture files on Windows.
            server.daemon_threads = False
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.up.release.set()
        for server in (self.server, self.up):
            server.shutdown(); server.server_close()
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.temp.cleanup()

    def post(self, body, headers=None):
        # High compression on shared Windows runners can exceed 15 seconds.
        # These fixtures verify usage semantics rather than request latency.
        client = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=60)
        client.request("POST", "/relay/" + self.server.settings["relay_token"] + "/responses", body,
                       {"Content-Type": "application/json", "session_id": "usage-test", **(headers or {})})
        return client, client.getresponse()

    def api(self, value=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=15)
        try:
            client.request("GET" if value is None else "POST", "/api/status" if value is None else "/api/settings",
                           None if value is None else json.dumps(value), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_one_switch_persists_both_protections_and_derives_the_threshold(self):
        for enabled, budget in ((False, 1_000_000), (True, 700_000), (True, 950_000)):
            status, result = self.api({"packet_size_protection_enabled": enabled, "budget_bytes": budget})
            self.assertEqual(status, 200)
            for key in ("packet_size_protection_enabled", "overflow_compaction_enabled", "native_compact_trigger_enabled"):
                self.assertEqual(result[key], enabled)
            self.assertEqual(result["native_compact_trigger_bytes"], budget - 50000)
            saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
            self.assertEqual(saved["packet_size_protection_enabled"], enabled)
            self.assertEqual(saved["budget_bytes"], budget)
            self.assertNotIn("overflow_compaction_enabled", saved)
            self.assertNotIn("native_compact_trigger_enabled", saved)

    def test_custom_budget_takes_effect_on_next_request_and_disable_stops_both(self):
        body = request_body(text_bytes=1_300_000)
        for budget, enabled, expected_input in ((700000, True, 500000), (800000, True, USAGE["input_tokens"]),
                                               (600000, False, USAGE["input_tokens"])):
            self.assertEqual(self.api({"packet_size_protection_enabled": enabled, "budget_bytes": budget})[0], 200)
            client, response = self.post(body)
            try:
                payload = response.read()
                value = json.loads(payload[len(DELTA):].split(b"data: ", 1)[1].strip())
                self.assertEqual(value["response"]["usage"]["input_tokens"], expected_input)
            finally:
                client.close()
            self.assertEqual(self.up.requests[-1][0], body)

    def test_both_auto_levels_apply_prediction_and_high_phase_does_not_oscillate(self):
        import base64
        import random
        import zstandard
        rng = random.Random(19)
        a, b = (base64.b64encode(rng.randbytes(300000)).decode() for _ in range(2))
        body = json.dumps({'input': a + b + a}).encode()
        fast_size = len(zstandard.ZstdCompressor(level=1).compress(body))
        high_size = len(zstandard.ZstdCompressor(level=16).compress(body))
        budget = fast_size * 100 // 97
        self.assertGreaterEqual(fast_size, budget - 50000)
        self.assertLess(high_size, budget * .8)  # Reproduces the old downgrade.
        self.assertEqual(self.api({'auto_zstd_level_enabled': True, 'budget_bytes': budget})[0], 200)
        self.server.settings['auto_detect_providers'] = True
        high_wire = json.dumps({'input': base64.b64encode(rng.randbytes(900000)).decode()}).encode()
        samples = ((b'{"input":"initial small context"}', 0, USAGE['input_tokens']),
                   (body, 1, 500000), (body, 17, USAGE['input_tokens']),
                   (high_wire, 17, 500000), (b'{"input":"native compacted context"}', 0, USAGE['input_tokens']),
                   (body, 1, 500000))
        for raw, phase, tokens in samples:
            with self.subTest(phase=phase, tokens=tokens):
                client, response = self.post(raw)
                try:
                    self.assertEqual(response.status, 200)
                    value = json.loads(response.read()[len(DELTA):].split(b'data: ', 1)[1].strip())
                    self.assertEqual(value['response']['usage']['input_tokens'], tokens)
                finally:
                    client.close()
                meta = self.server.store.list(limit=1)[0]
                self.assertEqual(meta['relay_auto_zstd_phase'], phase)
                self.assertEqual(meta['relay_auto_zstd_next_phase'], 17 if phase else 0)
                self.assertEqual(meta['relay_zstd_level'], 16 if phase == 17 else phase or None)
                if phase == 1:
                    self.assertTrue(meta['usage_override_applied'])
                    self.assertEqual(meta['upstream_usage'], USAGE)
                self.assertNotIn('relay_auto_zstd_prediction_deferred', meta)
                self.assertEqual(self.up.requests[-1][0], raw)

    def test_real_summary_and_cache_reuse_still_rewrite_usage_using_original_size(self):
        from tests.test_overflow_compaction import message
        self.assertEqual(self.api({"packet_size_protection_enabled": True, "budget_bytes": 700000})[0], 200)
        items = [message(i, 140000) for i in range(18)]
        for cached in (False, True):
            body = json.dumps({"model": "test", "stream": True, "input": items,
                               "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'}}).encode()
            client, response = self.post(body, {"X-Test-Missing-Type": "1"})
            try:
                self.assertEqual(response.status, 200)
                value = json.loads(response.read()[len(DELTA):].split(b"data: ", 1)[1].strip())
                self.assertEqual(value["response"]["usage"]["input_tokens"], 500000)
            finally:
                client.close()
            packet = self.server.store.list(limit=1)[0]
            self.assertGreater(packet["usage_override_request_bytes"], 700000)
            self.assertEqual(packet["usage_override_trigger_bytes"], 650000)
            self.assertLessEqual(packet["overflow_final_wire_bytes"], 700000)
            self.assertEqual(packet["overflow_cache_hit"], cached)
            self.assertEqual(packet["overflow_compaction_calls"] > 0, not cached)
            self.assertTrue(packet["usage_override_applied"])
            self.assertEqual(packet["upstream_usage"], USAGE)
            items.append(message("next", 100))

    def test_invalid_combined_settings_are_atomic(self):
        before = dict(self.server.settings)
        saved = self.server.settings_path.read_bytes()
        for data in ({"packet_size_protection_enabled": "true"},
                     {"packet_size_protection_enabled": False, "budget_bytes": 50000},
                     {"packet_size_protection_enabled": False, "budget_bytes": True},
                     {"budget_bytes": 700000.5}, {"budget_bytes": 134217729},
                     {"packet_size_protection_enabled": False, "native_compact_trigger_enabled": True}):
            self.assertEqual(self.api(data)[0], 400)
            self.assertEqual(self.server.settings, before)
            self.assertEqual(self.server.settings_path.read_bytes(), saved)

    def test_activation_reloads_latest_legacy_settings_and_persists_one_switch(self):
        self.api({"packet_size_protection_enabled": False})
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        saved.pop("packet_size_protection_enabled")
        saved.update(overflow_compaction_enabled=False, native_compact_trigger_enabled=True, budget_bytes=720000)
        self.server.settings_path.write_text(json.dumps(saved), encoding="utf8")
        with patch.object(self.server, "sync_providers"):
            self.server.worker_control({"action": "activate", "maintenance": False})
        self.assertTrue(self.server.settings["packet_size_protection_enabled"])
        self.assertEqual(self.server.status()["native_compact_trigger_bytes"], 670000)
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        self.assertNotIn("overflow_compaction_enabled", saved)
        self.assertNotIn("native_compact_trigger_enabled", saved)

    def test_live_stream_keeps_delta_and_right_panel_uses_original_usage(self):
        body = request_body()
        client, response = self.post(body, {"X-Test-Hold": "1", "X-Test-Missing-Type": "1"})
        try:
            self.assertEqual(response.read(len(DELTA)), DELTA)
            self.assertIsNone(response.getheader("Content-Length"))
            self.assertIsNone(response.getheader("ETag"))
            self.up.release.set()
            result = json.loads(response.read().split(b"data: ", 1)[1].strip())
            self.assertEqual(result["response"]["usage"]["input_tokens"], 500000)
        finally:
            self.up.release.set(); client.close()
        self.assertEqual(self.up.requests[0], (body, "identity"))
        packet = self.server.store.list(limit=1)[0]
        self.assertGreaterEqual(packet["usage_override_request_bytes"], TRIGGER_BYTES)
        self.assertEqual(len(self.up.requests), 1)
        self.assertEqual(packet["upstream_usage"], USAGE)
        with patch.object(self.server.token_windows, "lookup", side_effect=AssertionError("History must not be read")):
            actual = self.server.token_windows.lookup_ready(packet)
        self.assertEqual(actual["input_tokens"], USAGE["input_tokens"])
        self.assertNotIn("window_tokens", actual)
        self.assertEqual(actual["source"], "upstream_response")

    def test_small_encoded_packets_disabled_setting_and_native_compaction_are_unchanged(self):
        for body, enabled in ((request_body(compressible=True), True),
                              (request_body(kind="compaction"), True), (request_body(), False)):
            self.server.settings["packet_size_protection_enabled"] = enabled
            client, response = self.post(body)
            try:
                self.assertEqual(response.read(), DELTA + sse(completed()))
                self.assertIsNotNone(response.getheader("Content-Length"))
            finally:
                client.close()

    def test_unprotected_response_records_real_usage_without_changing_stream_or_headers(self):
        self.server.settings["packet_size_protection_enabled"] = False
        client, response = self.post(request_body(compressible=True), {"X-Test-Hold": "1"})
        try:
            self.assertEqual(response.read(len(DELTA)), DELTA)
            self.assertEqual(response.getheader("ETag"), '"old-body"')
            self.assertEqual(int(response.getheader("Content-Length")), len(DELTA + sse(completed())))
            self.up.release.set()
            self.assertEqual(response.read(), sse(completed()))
        finally:
            self.up.release.set()
            client.close()
        packet = self.server.store.list(limit=1)[0]
        self.assertEqual(packet["upstream_usage"], USAGE)
        self.assertFalse(packet.get("usage_override_applied", False))
        self.assertNotIn("reported_usage", packet)


class ProtectionMigrationTests(unittest.TestCase):
    def test_fresh_install_default_and_saved_choices_survive_restart(self):
        cases = (
            (None, False),
            ({}, False),
            ({"packet_size_protection_enabled": False}, False),
            ({"packet_size_protection_enabled": True}, True),
            ({"packet_size_protection_enabled": False, "overflow_compaction_enabled": True}, False),
        )
        for settings, expected in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = root / "config.toml"; config.write_text('model="test"', encoding="utf8")
                original = config.read_bytes()
                data = root / "data"; data.mkdir()
                if settings is not None:
                    (data / "settings.json").write_text(json.dumps(settings), encoding="utf8")
                for _ in range(2):
                    server = InspectorServer(("127.0.0.1", 0), data, config, "https://example.invalid/v1")
                    try:
                        self.assertIs(server.settings["packet_size_protection_enabled"], expected)
                        saved = json.loads(server.settings_path.read_text(encoding="utf8"))
                        self.assertIs(saved["packet_size_protection_enabled"], expected)
                        self.assertNotIn("overflow_compaction_enabled", saved)
                        self.assertNotIn("native_compact_trigger_enabled", saved)
                        self.assertEqual(config.read_bytes(), original)
                    finally:
                        server.server_close()

    def test_legacy_settings_migrate_to_one_persisted_switch(self):
        for overflow, native, expected in ((False, False, False), (True, False, True),
                                           (False, True, True), (True, True, True)):
            with self.subTest(overflow=overflow, native=native), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = root / "config.toml"; config.write_text('model="test"', encoding="utf8")
                data = root / "data"; data.mkdir()
                (data / "settings.json").write_text(json.dumps({"overflow_compaction_enabled": overflow,
                    "native_compact_trigger_enabled": native, "budget_bytes": 750000}), encoding="utf8")
                server = InspectorServer(("127.0.0.1", 0), data, config, "https://example.invalid/v1")
                try:
                    self.assertEqual(server.settings["packet_size_protection_enabled"], expected)
                    saved = json.loads(server.settings_path.read_text(encoding="utf8"))
                    self.assertNotIn("overflow_compaction_enabled", saved)
                    self.assertNotIn("native_compact_trigger_enabled", saved)
                    self.assertEqual(server.status()["native_compact_trigger_bytes"], 700000)
                finally:
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
