"""State, protocol and relay regressions for optional incremental transport."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packet_inspector.incremental import (IncrementalTransport, IncrementalFailure,
                                          prepare, serialize, connect_socket, CONNECTION_SECONDS)
from tests import test_packet_inspector as fixtures


ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
HEADERS = {"Authorization": "Bearer synthetic-test-token", "session_id": "chat-one"}


def message(text, role="user"):
    return {"type": "message", "role": role, "content": [
        {"type": "output_text" if role == "assistant" else "input_text", "text": text}]}


def body(items, **fields):
    return {"model": "test-model", "instructions": "Retain every item.",
            "stream": True, "store": False, "tools": [], "input": items, **fields}


class Socket:
    """Independent stateful server double with connection-local response IDs."""
    def __init__(self, behavior=None):
        self.cache, self.frames, self.contexts, self.events = {}, [], [], []
        self.state = SimpleNamespace(name="OPEN")
        self.response = SimpleNamespace(headers={"x-request-id": "synthetic-request"})
        self.behavior = behavior
        self.generation_count = 0

    def send(self, raw):
        if self.state.name != "OPEN":
            raise ConnectionError("Socket closed")
        frame = json.loads(raw)
        self.frames.append(frame)
        if self.behavior and self.behavior(self, frame):
            return
        previous = frame.get("previous_response_id")
        if previous and previous not in self.cache:
            self.events.append({"type": "error", "status": 400,
                                "error": {"code": "previous_response_not_found"}})
            return
        context = deepcopy(self.cache.get(previous, []) + frame["input"])
        self.contexts.append(context)
        identifier = "resp_" + str(len(self.frames))
        output = []
        if frame.get("generate") is not False:
            self.generation_count += 1
            output = [dict(message("answer-" + str(self.generation_count), "assistant"),
                           id="msg_generated", status="completed")]
        response = {"id": identifier, "status": "completed", "output": output,
                    "usage": {"input_tokens": 1234, "output_tokens": 2, "total_tokens": 1236}}
        self.cache[identifier] = deepcopy(context + output)
        self.events.extend([{"type": "response.created", "response": {"id": identifier, "status": "in_progress"}},
                            {"type": "response.completed", "response": response}])

    def recv(self, timeout):
        if not self.events:
            raise ConnectionError("Connection interrupted during generation")
        return json.dumps(self.events.pop(0))

    def close(self):
        self.state.name = "CLOSED"
        self.cache.clear()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.sockets, self.meta = [], {}
        self.behavior = None
        self.now = 0
        self.transport = IncrementalTransport(connector=self.connect, clock=lambda: self.now)
        self.addCleanup(self.transport.clear)

    def connect(self, *_args):
        socket = Socket(self.behavior)
        self.sockets.append(socket)
        return socket

    def run_request(self, value, *, budget=100_000, headers=None, session="chat-one", scope=""):
        headers = headers or HEADERS
        plan = prepare(ENDPOINT, serialize(value), "identity", headers, session, budget, scope=scope)
        self.assertIsNotNone(plan)
        with self.transport.open(plan, headers, {}, None, lambda **fields: self.meta.update(fields), timeout=1) as response:
            data = response.read()
            if response.status != 200:
                return response.status, json.loads(data)
        events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]
        return response.status, events[-1]["response"]

    def test_delta_retains_history_without_reupload_and_preserves_usage(self):
        first = body([message("first")], future_parameter={"keep": "unchanged"})
        _, response = self.run_request(first)
        output = deepcopy(response["output"])
        output[0].pop("id")
        output[0].pop("status")
        second = body(first["input"] + output + [message("next")], future_parameter={"keep": "unchanged"})
        _, response = self.run_request(second)
        socket = self.sockets[0]
        self.assertEqual(len(self.sockets), 1)
        self.assertEqual(socket.frames[-1]["input"], [message("next")])
        self.assertEqual(socket.frames[-1]["future_parameter"], {"keep": "unchanged"})
        self.assertEqual(socket.contexts[-1], first["input"] + socket.cache["resp_1"][1:] + [message("next")])
        self.assertEqual(response["usage"]["input_tokens"], 1234)
        self.assertEqual(self.meta["incremental_reused_items"], 2)

    def test_large_context_is_rehydrated_losslessly_without_extra_generation(self):
        items = [message("part-" + str(n) + " opaque-text " * 35) for n in range(9)]
        _, response = self.run_request(body(items), budget=1050)
        socket = self.sockets[0]
        self.assertGreater(len(socket.frames), 2)
        self.assertTrue(all(len(serialize(frame)) <= 1050 for frame in socket.frames))
        self.assertTrue(all(frame.get("generate") is False for frame in socket.frames[:-1]))
        self.assertNotIn("generate", socket.frames[-1])
        self.assertEqual(socket.frames[-1]["input"], items[-1:])
        self.assertEqual(socket.contexts[-1], items)
        self.assertEqual(socket.generation_count, 1)
        self.assertEqual(self.meta["incremental_bootstrap_requests"], len(socket.frames) - 1)
        self.assertEqual(response["usage"]["input_tokens"], 1234)

    def test_native_compaction_trigger_is_only_sent_with_the_actual_generation(self):
        items = [message("old context " * 30) for _ in range(7)] + [{"type": "compaction_trigger"}]
        self.run_request(body(items, include=["output.encrypted_content"]), budget=900)
        socket = self.sockets[0]
        self.assertGreater(len(socket.frames), 2)
        self.assertTrue(all(not any(item.get("type") == "compaction_trigger" for item in frame["input"])
                            for frame in socket.frames[:-1]))
        self.assertEqual(socket.frames[-1]["input"], [{"type": "compaction_trigger"}])
        self.assertEqual(socket.contexts[-1], items)
        self.assertEqual(socket.generation_count, 1)

    def test_per_turn_metadata_and_delivery_options_do_not_reset_native_context(self):
        first = body([message("first")], client_metadata={"turn_id": "turn-one"},
                     stream_options={"reasoning_summary_delivery": "concurrent"})
        _, response = self.run_request(first)
        history = deepcopy(first["input"] + response["output"])
        for item in history:
            item["internal_chat_message_metadata_passthrough"] = {"turn_id": "turn-one"}
        second = body(history + [message("next")], client_metadata={"turn_id": "turn-two"},
                      stream_options={"reasoning_summary_delivery": "sequential_cutoff"})
        self.run_request(second)
        self.assertEqual(len(self.sockets), 1)
        self.assertEqual(self.sockets[0].frames[-1]["input"], [message("next")])
        self.assertEqual(self.sockets[0].frames[-1]["client_metadata"], {"turn_id": "turn-two"})
        self.assertEqual(self.meta["incremental_reused_items"], 2)

    def test_late_tool_result_metadata_and_unknown_parameter_changes_reset_context(self):
        tool = {"type": "function_call", "call_id": "call_one", "name": "test", "arguments": "{}"}
        result = {"type": "function_call_output", "call_id": "call_one", "output": "pending",
                  "internal_chat_message_metadata_passthrough": {"complete": False}}
        first = body([message("first"), tool, result], future_context_parameter="old")
        _, response = self.run_request(first)
        history = deepcopy(first["input"] + response["output"])
        history[2]["internal_chat_message_metadata_passthrough"]["complete"] = True
        self.run_request(body(history + [message("next")], future_context_parameter="old"))
        self.assertEqual(len(self.sockets), 2)
        self.run_request(body(history + [message("next")], future_context_parameter="changed"))
        self.assertEqual(len(self.sockets), 3)

    def test_tool_pairs_reasoning_compaction_and_native_notices_survive_rehydration(self):
        items = [message("old " * 80),
                 {"type": "compaction", "encrypted_content": "opaque-compaction-unchanged"},
                 {"type": "reasoning", "encrypted_content": "opaque-reasoning-unchanged", "summary": []},
                 {"type": "function_call", "call_id": "call_one", "name": "test", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "call_one", "output": "result"},
                 message("new " * 80)]
        self.run_request(body(items), budget=1050)
        socket = self.sockets[0]
        self.assertEqual(socket.contexts[-1], items)
        for frame in socket.frames:
            kinds = [item["type"] for item in frame["input"]]
            self.assertEqual("function_call" in kinds, "function_call_output" in kinds)

    def test_expired_id_reconnects_and_recovers_once_before_generation(self):
        first = body([message("old")])
        _, response = self.run_request(first)
        self.sockets[0].cache.clear()
        items = first["input"] + response["output"] + [message("next")]
        self.run_request(body(items))
        self.assertEqual(len(self.sockets), 2)
        self.assertEqual(self.sockets[0].state.name, "CLOSED")
        self.assertEqual(self.sockets[1].contexts[-1], items)
        self.assertEqual(sum(socket.generation_count for socket in self.sockets), 2)
        self.assertEqual(self.meta["incremental_recoveries"], 1)

    def test_nested_failed_event_can_report_an_expired_cache_before_generation(self):
        first = body([message("old")])
        _, response = self.run_request(first)

        def expired(socket, frame):
            socket.events.append({"type": "response.failed", "response": {"status": "failed",
                "error": {"code": "previous_response_not_found"}}})
            return True

        self.sockets[0].behavior = expired
        items = first["input"] + response["output"] + [message("next")]
        self.run_request(body(items))
        self.assertEqual(len(self.sockets), 2)
        self.assertEqual(self.sockets[-1].contexts[-1], items)
        self.assertEqual(self.meta["incremental_recoveries"], 1)

    def test_codex_unstructured_cache_miss_is_recognized_but_other_400_is_not(self):
        def fail(socket, frame):
            if frame.get("previous_response_id"):
                socket.events.append({"type": "error", "error": {"type": "invalid_request_error",
                    "message": "Invalid `previous_response_id`."}})
                return True
            return False
        _, response = self.run_request(body([message("old")]))
        self.sockets[0].behavior = fail
        self.run_request(body([message("old")] + response["output"] + [message("next")]))
        self.assertEqual(self.meta["incremental_recoveries"], 1)
        self.behavior = lambda socket, frame: (socket.events.append(
            {"type": "error", "status": 400, "error": {"message": "Unsupported parameter: future_parameter"}}) or True)
        status, response = self.run_request(body([message("independent")]), session="other")
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["message"], "Unsupported parameter: future_parameter")
        self.assertEqual(self.meta["incremental_recoveries"], 0)

    def test_repeated_cache_failure_has_a_finite_recovery_budget(self):
        self.behavior = lambda socket, frame: (socket.events.append(
            {"type": "error", "error": {"code": "previous_response_not_found"}}) or True)
        with self.assertRaisesRegex(IncrementalFailure, "重建后"):
            self.run_request(body([message("text")]))
        self.assertEqual(len(self.sockets), 2)
        self.assertTrue(all(socket.state.name == "CLOSED" for socket in self.sockets))

    def test_closed_and_aged_connections_reopen_before_sending_new_input(self):
        for mode in ("closed", "aged"):
            with self.subTest(mode=mode):
                self.transport.clear()
                _, response = self.run_request(body([message("old")]))
                socket = self.sockets[-1]
                if mode == "closed":
                    socket.close()
                else:
                    self.now += CONNECTION_SECONDS + 1
                self.run_request(body([message("old")] + response["output"] + [message("next")]))
                self.assertIsNot(socket, self.sockets[-1])
                self.assertEqual(socket.state.name, "CLOSED")

    def test_native_compact_history_and_changed_parameters_reset_the_cache(self):
        self.run_request(body([message("old")]))
        compacted = [{"type": "compaction", "encrypted_content": "canonical-native-window"}, message("next")]
        self.run_request(body(compacted))
        self.assertEqual(self.sockets[-1].contexts[-1], compacted)
        self.assertEqual(len(self.sockets), 2)
        self.run_request(body(compacted, model="new-model"))
        self.assertEqual(len(self.sockets), 3)

    def test_accounts_sessions_and_egress_changes_never_share_ids(self):
        value = body([message("private")])
        self.run_request(value)
        self.run_request(value, session="other")
        self.run_request(value, headers={**HEADERS, "Authorization": "Bearer other"})
        self.run_request(value, headers={**HEADERS, "OpenAI-Project": "other-project"})
        self.run_request(value, scope="changed-proxy")
        self.assertEqual(len(self.sockets), 5)
        self.assertTrue(all("previous_response_id" not in socket.frames[0] for socket in self.sockets))

    def test_turn_interruption_does_not_silently_replay_generation(self):
        def interrupt(socket, frame):
            socket.events.append({"type": "response.created", "response": {"id": "incomplete"}})
            return True
        self.behavior = interrupt
        with self.assertRaises(ConnectionError):
            self.run_request(body([message("question")]))
        self.assertEqual(len(self.sockets), 1)
        self.assertEqual(len(self.sockets[0].frames), 1)
        self.assertEqual(self.sockets[0].state.name, "CLOSED")

    def test_clear_does_not_interrupt_an_active_response_and_reenable_starts_fresh(self):
        value = body([message("question")])
        plan = prepare(ENDPOINT, serialize(value), "identity", HEADERS, "chat-one", 100_000)
        response = self.transport.open(plan, HEADERS, {}, None, lambda **fields: None, timeout=1)
        socket = self.sockets[-1]
        self.transport.clear()
        self.assertEqual(socket.state.name, "OPEN")
        response.read()
        response.close()
        self.assertEqual(socket.state.name, "CLOSED")
        self.run_request(value)
        self.assertEqual(len(self.sockets), 2)
        self.assertNotIn("previous_response_id", self.sockets[-1].frames[0])

    def test_disabling_before_a_prepared_request_opens_does_not_retain_its_socket(self):
        value = body([message("question")])
        plan = prepare(ENDPOINT, serialize(value), "identity", HEADERS, "chat-one", 100_000)
        self.transport.clear()
        with self.transport.open(plan, HEADERS, {}, None, lambda **fields: None, timeout=1,
                                 retain=lambda: False) as response:
            response.read()
        self.assertEqual(self.sockets[0].state.name, "CLOSED")

    def test_handshake_errors_keep_the_upstream_status_and_response_body(self):
        error = RuntimeError("Handshake rejected")
        error.response = SimpleNamespace(status_code=403, body=b'{"error":{"message":"Proxy denied"}}',
                                         headers={"content-type": "application/json", "x-request-id": "denied-request"})
        self.transport.connector = lambda *_args: (_ for _ in ()).throw(error)
        plan = prepare(ENDPOINT, serialize(body([message("question")])), "identity", HEADERS, "chat-one", 100_000)
        with self.transport.open(plan, HEADERS, {}, None, lambda **fields: None, timeout=1) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual(response.headers["x-request-id"], "denied-request")
            self.assertEqual(json.loads(response.read())["error"]["message"], "Proxy denied")

    def test_atomic_oversize_and_unsupported_warmup_fail_without_generating(self):
        with self.assertRaisesRegex(IncrementalFailure, "单个上下文"):
            self.run_request(body([message("large" * 500)]), budget=800)
        self.assertEqual(self.sockets[-1].generation_count, 0)

        def ignored_warmup(socket, frame):
            socket.events.append({"type": "response.completed", "response": {
                "id": "bad-warmup", "status": "completed", "output": [message("unexpected", "assistant")]}})
            return True
        self.behavior = ignored_warmup
        with self.assertRaisesRegex(IncrementalFailure, "无生成模式"):
            self.run_request(body([message("large " * 50)] * 5), budget=800)
        self.assertEqual(len(self.sockets[-1].frames), 1)

    def test_pool_is_bounded_and_idle_connections_are_collected(self):
        self.transport.max_sessions = 2
        for index in range(4):
            self.run_request(body([message(str(index))]), session=str(index))
        self.assertEqual(len(self.transport.sessions), 2)
        self.assertTrue(all(socket.state.name == "CLOSED" for socket in self.sockets[:2]))
        self.now += 301
        self.transport.prune()
        self.assertFalse(self.transport.sessions)

    def test_unmanaged_protocols_and_server_context_requests_are_untouched(self):
        good = body([message("test")])
        for fields in ({"store": True}, {"stream": False}, {"previous_response_id": "old"},
                       {"conversation": "server-conversation"}, {"background": True}, {"input": "text"},
                       {"context_management": [{"type": "compaction", "compact_threshold": 1000}]},
                       {"truncation": "auto"}):
            self.assertIsNone(prepare(ENDPOINT, serialize({**good, **fields}), "identity", HEADERS, "id", 1000))
        for endpoint in ("https://other.example/responses", ENDPOINT + "/compact", "http://127.0.0.1/responses"):
            self.assertIsNone(prepare(endpoint, serialize(good), "identity", HEADERS, "id", 1000))

    def test_proxy_bypass_and_tls_use_the_existing_transport_settings(self):
        context = object()
        for bypass, proxy in ((False, "http://regional-proxy.test:8080"),
                              (False, "https://secure-proxy.test:8443"), (True, "http://regional-proxy.test:8080")):
            with self.subTest(bypass=bypass, proxy=proxy), \
                    patch("packet_inspector.incremental.proxy_bypass", return_value=bypass), \
                    patch("websockets.sync.client.connect") as connect:
                connect_socket(ENDPOINT, {**HEADERS, "Content-Encoding": "zstd", "Content-Length": "999"},
                               {"https": proxy}, context, 20)
                args, kw = connect.call_args
                self.assertEqual(args, (ENDPOINT.replace("https:", "wss:", 1),))
                self.assertIs(kw["ssl"], context)
                self.assertEqual(kw["proxy"], None if bypass else proxy)
                self.assertIs(kw["proxy_ssl"], context if proxy.startswith("https:") and not bypass else None)
                self.assertFalse(any(key.lower() in {"content-encoding", "content-length"} for key in kw["additional_headers"]))


class RelaySwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    api = fixtures.RelayTests.api
    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest

    def setUp(self):
        self.previous = dict(self.server.settings)
        self.server.save_settings({**self.previous, "auto_detect_providers": True,
                                   "incremental_transport_enabled": False, "packet_size_protection_enabled": False})
        self.sockets = []
        original = self.server.incremental
        self.server.incremental = IncrementalTransport(connector=self.connect)
        self.addCleanup(lambda: setattr(self.server, "incremental", original))
        self.addCleanup(self.server.incremental.clear)
        self.addCleanup(lambda: self.server.save_settings(self.previous))

    def connect(self, *_args):
        socket = Socket()
        self.sockets.append(socket)
        return socket

    def test_saved_switch_changes_the_very_next_request_without_config_or_restart(self):
        config = self.config.read_bytes()
        raw = serialize(body([message("question")]))
        with patch("packet_inspector.server.prepare_incremental", side_effect=lambda endpoint, *args, **kw: prepare(ENDPOINT, *args, **kw)), \
                patch.object(self.server.local_opener, "inspector_proxies", {}, create=True):
            count = len(self.up.requests)
            self.assertEqual(self.relay(raw, headers=HEADERS)[0], 200)
            self.assertEqual(len(self.up.requests), count + 1)
            self.assertFalse(self.sockets)
            self.assertEqual(self.api("/api/settings", {"incremental_transport_enabled": True})[0], 200)
            self.assertEqual(self.relay(raw, headers=HEADERS)[0], 200)
            self.assertEqual(len(self.up.requests), count + 1)
            meta, _ = self.latest()
            self.assertEqual(meta["incremental_transport"], "websocket")
            self.assertEqual(meta["upstream_usage"]["input_tokens"], 1234)
            self.assertEqual(meta["outbound_wire_bytes"], len(serialize(self.sockets[0].frames[-1])))
            self.assertEqual(self.api("/api/settings", {"incremental_transport_enabled": False})[0], 200)
            self.assertEqual(self.relay(raw, headers=HEADERS)[0], 200)
            self.assertEqual(len(self.up.requests), count + 2)
            self.assertEqual(self.sockets[0].state.name, "CLOSED")
            self.assertEqual(self.api("/api/settings", {"incremental_transport_enabled": True})[0], 200)
            self.assertEqual(self.relay(raw, headers=HEADERS)[0], 200)
            self.assertEqual(len(self.sockets), 2)
            self.assertEqual(self.config.read_bytes(), config)

    def test_settings_validate_and_persist_without_touching_other_preferences(self):
        status, result = self.api("/api/status")
        self.assertTrue(result["incremental_transport_supported"])
        self.assertFalse(result["incremental_transport_enabled"])
        for invalid in ("true", 1, None):
            self.assertEqual(self.api("/api/settings", {"incremental_transport_enabled": invalid})[0], 400)
        status, result = self.api("/api/settings", {"incremental_transport_enabled": True})
        self.assertEqual(status, 200)
        saved = json.loads(self.server.settings_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["incremental_transport_enabled"])
        self.assertEqual(saved["zstd_level"], self.previous["zstd_level"])


if __name__ == "__main__":
    unittest.main()
