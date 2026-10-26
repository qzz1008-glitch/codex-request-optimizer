"""Overflow behavior tests; no real model requests or user chat mutations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".local" / "builddeps"))

from packet_inspector.analyzer import JsonBytes, decode_body, encode
from packet_inspector.overflow import (CheckpointStream, OverflowCompactor, OverflowFailure,
                                       CHECKPOINT_PREFIX, CHECKPOINT_PROMPT, digest,
                                       encode_changed, rebuild_prefix, safe_cuts, truncate_user_parts)
from packet_inspector.server import InspectorServer
from packet_inspector.transport import prepare_request


def message(seed, size=550, role="assistant"):
    return {"type": "message", "role": role,
            "content": [{"type": "input_text" if role != "assistant" else "output_text",
                         "text": hashlib.shake_256(str(seed).encode()).hexdigest(size // 2)}]}


def make_body(items, **extra):
    value = {"model": "test", "stream": True, "input": items, "store": False, **extra}
    return json.dumps(value, ensure_ascii=True, indent=1).encode()


def complete(summary="Checkpoint: preserve reference 7319."):
    return b"data: " + encode({"type": "response.completed", "response": {
        "status": "completed", "output": [{"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": summary}]}]}}) + b"\n\n"


class OverflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compactor = OverflowCompactor(self.root)
        self.calls = []
        self.items = [message(i) for i in range(14)]
        self.headers = {"session_id": "chat-one", "Authorization": "Bearer test-never-cache"}

    def compact(self, body, encoding):
        self.assertLessEqual(len(body), self.budget)
        value = json.loads(decode_body(body, encoding))
        self.assertNotIn("tools", value)
        self.assertFalse(value["store"])
        self.assertFalse(any(i.get("type") == "additional_tools" for i in value["input"]))
        self.calls.append(value)
        return "Checkpoint #" + str(len(self.calls)) + ": preserve reference 7319."

    def run_prepare(self, body=None, headers=None, budget=4000, mode="off", compactor=None, compact=None, upstream="https://example.invalid/v1"):
        self.budget = budget
        headers = self.headers if headers is None else headers
        body = make_body(self.items) if body is None else body
        original, encoding = prepare_request(body, headers, "POST", "/responses", mode)
        return (compactor or self.compactor).prepare(original, encoding, body, headers,
            "/responses", upstream, mode, budget, compact or self.compact)

    def rows(self):
        with closing(sqlite3.connect(self.compactor.path)) as db:
            return db.execute("SELECT key,scope,covered,fingerprint,output FROM mappings ORDER BY key").fetchall()

    def test_under_budget_preserves_wire_and_creates_no_cache(self):
        raw = make_body([message(1, 30)])
        wire, encoding, meta = self.run_prepare(raw)
        self.assertIs(wire, raw)
        self.assertEqual(encoding, "identity")
        self.assertFalse(self.calls)
        self.assertFalse(self.compactor.path.exists())
        self.assertFalse(meta["overflow_cache_reset"])

    def test_lite_reasoning_context_survives_every_checkpoint(self):
        reasoning = {"effort": "max", "summary": "detailed", "context": "all_turns"}
        _, _, meta = self.run_prepare(make_body(self.items, reasoning=reasoning))
        self.assertGreater(meta["overflow_compaction_calls"], 0)
        self.assertTrue(self.calls)
        self.assertTrue(all(call["reasoning"] == reasoning for call in self.calls))

    def test_checkpoint_follows_native_extensions_without_mutating_original(self):
        original = {"model": "test", "input": [message(1)],
                    "reasoning": {"effort": "max", "context": "future-native-mode", "future_option": {"enabled": True}},
                    "include": ["reasoning.encrypted_content", "future.output"],
                    "future_protocol": {"mode": "native"}, "service_tier": "auto",
                    "text": {"verbosity": "low", "format": {"type": "json_object"}},
                    "previous_response_id": "old-response", "conversation": "old-conversation",
                    "max_output_tokens": 1, "background": True,
                    "client_metadata": {"future_client": "native", "x-codex-turn-metadata": json.dumps({
                        "request_kind": "turn", "future_turn": {"enabled": True},
                        "compaction": {"future_compaction": "native"}})}}
        before = deepcopy(original)
        value = self.compactor._checkpoint_request(original, original["input"], "test-key")
        for name in ("reasoning", "include", "future_protocol", "service_tier"):
            self.assertEqual(value[name], original[name])
        self.assertEqual(value["text"], {"verbosity": "low"})
        for name in ("previous_response_id", "conversation", "max_output_tokens"):
            self.assertNotIn(name, value)
        self.assertFalse(value["background"])
        self.assertEqual(value["client_metadata"]["future_client"], "native")
        turn = json.loads(value["client_metadata"]["x-codex-turn-metadata"])
        self.assertEqual(turn["request_kind"], "compaction")
        self.assertEqual(turn["future_turn"], {"enabled": True})
        self.assertEqual(turn["compaction"]["future_compaction"], "native")
        value["reasoning"]["future_option"]["enabled"] = False
        value["input"][0]["content"][0]["text"] = "changed only in checkpoint"
        self.assertEqual(original, before)

    def test_first_overflow_fits_each_request_and_preserves_non_input_bytes(self):
        raw = make_body(self.items, tools=[{"type": "function", "name": "business_tool", "parameters": {"type": "object"}}],
                        instructions="Keep this exact instruction.", tool_choice="auto", text={"format": {"type": "json_object"}})
        wire, encoding, meta = self.run_prepare(raw)
        self.assertLessEqual(len(wire), 4000)
        self.assertGreater(meta["overflow_compaction_calls"], 0)
        before, after = JsonBytes(raw).spans["/input"], JsonBytes(wire).spans["/input"]
        self.assertEqual(raw[:before[0]], wire[:after[0]])
        self.assertEqual(raw[before[1]:], wire[after[1]:])
        self.assertEqual(json.loads(wire)["input"][-1], self.items[-1])
        self.assertNotIn(b"test-never-cache", self.compactor.path.read_bytes())

    def test_cache_reuses_prefix_and_retains_new_tail_without_recompacting(self):
        self.run_prepare()
        self.calls.clear()
        added = message("new", 80)
        wire, _, meta = self.run_prepare(make_body(self.items + [added]))
        self.assertTrue(meta["overflow_cache_hit"])
        self.assertEqual(meta["overflow_compaction_calls"], 0)
        self.assertFalse(self.calls)
        self.assertEqual(json.loads(wire)["input"][-1], added)
        self.assertEqual(len(self.rows()), 1)

    def test_regrowth_updates_mapping_and_restart_reuses_it(self):
        self.run_prepare()
        original_covered = self.rows()[0][2]
        bigger = make_body(self.items + [message(i) for i in range(100, 117)])
        wire, _, meta = self.run_prepare(bigger)
        self.assertTrue(meta["overflow_cache_hit"])
        self.assertGreater(meta["overflow_compaction_calls"], 0)
        self.assertGreater(self.rows()[0][2], original_covered)
        new_instance = OverflowCompactor(self.root)
        _, _, again = self.run_prepare(bigger, compactor=new_instance)
        self.assertTrue(again["overflow_cache_hit"])
        self.assertEqual(again["overflow_compaction_calls"], 0)

    def test_original_below_budget_clears_cache_and_keeps_body_identical(self):
        self.run_prepare()
        small = make_body([message("native-compaction", 60)])
        wire, _, meta = self.run_prepare(small)
        self.assertIs(wire, small)
        self.assertTrue(meta["overflow_cache_reset"])
        self.assertEqual(self.rows(), [])

    def test_changed_history_discards_mapping_instead_of_applying_wrong_prefix(self):
        self.run_prepare()
        changed = [message("changed")] + self.items[1:]
        _, _, meta = self.run_prepare(make_body(changed))
        self.assertFalse(meta["overflow_cache_hit"])
        self.assertTrue(meta["overflow_cache_reset"])
        self.assertGreater(meta["overflow_compaction_calls"], 0)

    def test_chat_account_model_and_upstream_are_isolated(self):
        self.run_prepare()
        for kwargs in [dict(headers={**self.headers, "session_id": "chat-two"}),
                       dict(headers={**self.headers, "Authorization": "Bearer other-account"}),
                       dict(body=make_body(self.items, model="other-model")),
                       dict(upstream="https://another.invalid/v1")]:
            _, _, meta = self.run_prepare(**kwargs)
            self.assertFalse(meta["overflow_cache_hit"])
            self.assertGreater(meta["overflow_compaction_calls"], 0)

    def test_catalog_and_developer_instructions_are_preserved(self):
        pinned = [{"type": "additional_tools", "role": "developer", "tools": [{"name": "keep_catalog"}]},
                  {"role": "developer", "content": "Preserve every byte of this instruction."}]
        wire, _, _ = self.run_prepare(make_body(pinned + self.items))
        self.assertEqual(json.loads(wire)["input"][:2], pinned)

    def test_parallel_tool_pairs_are_never_split(self):
        pairs = [message(1), {"type": "function_call", "call_id": "a", "name": "one", "arguments": "{}"},
                 {"type": "custom_tool_call", "call_id": "b", "name": "two", "input": "x"},
                 {"type": "function_call_output", "call_id": "a", "output": "done"},
                 {"type": "custom_tool_call_output", "call_id": "b", "output": "done"}, message(2)]
        self.assertEqual(safe_cuts(pairs), [1, 5])
        self.run_prepare(make_body(pairs + self.items))
        for request in self.calls:
            calls = [i.get("call_id") for i in request["input"] if i.get("type") in {"function_call", "custom_tool_call"}]
            outputs = [i.get("call_id") for i in request["input"] if i.get("type") in {"function_call_output", "custom_tool_call_output"}]
            self.assertEqual(sorted(calls), sorted(outputs))

    def test_delayed_notifications_stay_with_their_original_call(self):
        items = [message("before", 30),
                 {"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "notify('progress')"},
                 {"type": "custom_tool_call_output", "call_id": "a", "output": "primary result"}]
        for index in range(15):
            items.extend([message(index, 30), {"type": "custom_tool_call_output", "call_id": "a",
                "id": f"notice-{index}", "name": "exec", "output": f"progress {index}"}])
        items.append(message("latest", 30))
        self.assertEqual(safe_cuts(items), [1, len(items) - 1])

    def test_parallel_calls_with_multiple_outputs_are_not_split(self):
        items = [message("before", 30),
                 {"type": "function_call", "call_id": "a", "name": "one", "arguments": "{}"},
                 {"type": "custom_tool_call", "call_id": "b", "name": "exec", "input": "notify('x')"},
                 {"type": "function_call_output", "call_id": "a", "output": "one"},
                 {"type": "custom_tool_call_output", "call_id": "b", "output": "two"},
                 message("between", 30),
                 {"type": "function_call_output", "call_id": "a", "output": "more one"},
                 {"type": "custom_tool_call_output", "call_id": "b", "output": "more two"},
                 message("latest", 30)]
        self.assertEqual(safe_cuts(items), [1, 8])

    def test_latest_notification_group_is_retained(self):
        items = [message("before", 30),
                 {"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "x"},
                 {"type": "custom_tool_call_output", "call_id": "a", "output": "primary"},
                 message("between", 30),
                 {"type": "custom_tool_call_output", "call_id": "a", "name": "exec", "output": "later"}]
        self.assertEqual(safe_cuts(items), [1])

    def test_notifications_do_not_close_an_unfinished_parallel_call(self):
        items = [message("before", 30),
                 {"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "x"},
                 {"type": "custom_tool_call_output", "call_id": "a", "output": "primary"},
                 {"type": "function_call", "call_id": "b", "name": "pending", "arguments": "{}"},
                 {"type": "custom_tool_call_output", "call_id": "a", "name": "exec", "output": "later"},
                 message("latest", 30)]
        self.assertEqual(safe_cuts(items), [1])

    def test_results_before_their_call_and_missing_identifiers_are_rejected(self):
        cases = [[{"type": "custom_tool_call_output", "call_id": "a", "output": "out of order"},
                  {"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "x"}],
                 [{"type": "custom_tool_call", "name": "exec", "input": "x"}],
                 [{"type": "custom_tool_call_output", "output": "missing id"}]]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(OverflowFailure) as error:
                safe_cuts(items + [message("latest", 30)])
            self.assertEqual(error.exception.code, "overflow_tool_pairing")

    def test_compaction_keeps_all_sixteen_results_with_the_call(self):
        group = [{"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "notify('progress')"}]
        group.extend({"type": "custom_tool_call_output", "call_id": "a", "id": f"result-{index}",
                      "name": "exec", "output": f"result {index}"} for index in range(16))
        wire, _, _ = self.run_prepare(make_body([message("before", 30)] + group + self.items))
        self.assertTrue(any(any(item.get("call_id") == "a" for item in request["input"]) for request in self.calls))
        for items in [request["input"] for request in self.calls] + [json.loads(wire)["input"]]:
            matching = [item for item in items if item.get("call_id") == "a"]
            self.assertIn(matching, [[], group])
        self.assertEqual(json.loads(wire)["input"][-1], self.items[-1])

    def test_new_notification_invalidates_an_old_cached_cut(self):
        original = [message("before", 30),
                    {"type": "custom_tool_call", "call_id": "a", "name": "exec", "input": "x"},
                    {"type": "custom_tool_call_output", "call_id": "a", "output": "primary"},
                    message("large-one", 2000), message("large-two", 2000), message("tail", 600)]
        self.run_prepare(make_body(original))
        covered = self.rows()[0][2]
        self.assertGreater(covered, 2)
        self.assertLess(covered, len(original))
        notification = {"type": "custom_tool_call_output", "call_id": "a", "name": "exec", "output": "later"}
        extended = original[:covered] + [notification] + original[covered:]
        self.calls.clear()
        wire, _, meta = self.run_prepare(make_body(extended))
        self.assertFalse(meta["overflow_cache_hit"])
        self.assertTrue(meta["overflow_cache_reset"])
        expected = [original[1], original[2], notification]
        for items in [request["input"] for request in self.calls] + [json.loads(wire)["input"]]:
            matching = [item for item in items if item.get("call_id") == "a"]
            self.assertIn(matching, [[], expected])

    def test_invalid_pair_or_single_huge_item_fails_without_committing(self):
        bad = [{"type": "function_call_output", "call_id": "missing", "output": "done"}] + self.items
        for items in (bad, [message("huge", 12000)]):
            with self.assertRaises(OverflowFailure):
                self.run_prepare(make_body(items))
        self.assertFalse(self.compactor.path.exists())

    def test_failure_preserves_previously_working_mapping(self):
        self.run_prepare()
        saved = self.rows()
        def fail(*_):
            raise OverflowFailure("synthetic_failure", "synthetic")
        bigger = make_body(self.items + [message(i) for i in range(200, 220)])
        with self.assertRaises(OverflowFailure):
            self.run_prepare(bigger, compact=fail)
        self.assertEqual(self.rows(), saved)

    def test_existing_gzip_and_zstd_encodings_are_rebuilt_correctly(self):
        for encoding in ("gzip", "zstd"):
            with self.subTest(encoding=encoding):
                headers = {**self.headers, "Content-Encoding": encoding, "session_id": encoding}
                raw, _ = encode_changed(make_body(self.items), headers, "/responses", "off")
                wire, final_encoding, meta = self.run_prepare(raw, headers=headers, budget=2400)
                self.assertEqual(final_encoding, encoding)
                self.assertLessEqual(len(wire), 2400)
                self.assertEqual(json.loads(decode_body(wire, encoding))["input"][-1], self.items[-1])

    def test_real_one_million_limit_uses_final_zstd_bytes(self):
        items = [message(i, 140000) for i in range(18)]
        raw = make_body(items)
        before, _ = prepare_request(raw, self.headers, "POST", "/responses", "zstd")
        self.assertGreater(len(before), 1_000_000)
        wire, encoding, meta = self.run_prepare(raw, budget=1_000_000, mode="zstd")
        self.assertLessEqual(len(wire), 1_000_000)
        self.assertEqual(encoding, "zstd")
        self.assertGreater(meta["overflow_compaction_calls"], 0)

    def test_same_chat_concurrent_requests_share_the_new_mapping(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_prepare(), range(2)))
        self.assertEqual(sum(bool(r[2]["overflow_cache_hit"]) for r in results), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_original_user_constraints_survive_overflow_and_reuse(self):
        user = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Do not send messages. Keep port 47844 and reference 7319 unchanged."}]}
        items = [user] + self.items
        wire, _, meta = self.run_prepare(make_body(items))
        current = json.loads(wire)["input"]
        self.assertIn(user, current)
        self.assertEqual(current[0], user)
        self.assertTrue(any(i.get("role") == "user" and i["content"][0].get("text", "").startswith(CHECKPOINT_PREFIX)
                            for i in current))
        self.assertGreater(meta["overflow_compaction_calls"], 0)
        added = {"role": "user", "content": "Only inspect, do not execute."}
        reused, _, meta = self.run_prepare(make_body(items + [added]))
        self.assertTrue(meta["overflow_cache_hit"])
        self.assertIn(user, json.loads(reused)["input"])
        self.assertEqual(json.loads(reused)["input"][-1], added)

    def test_old_assistant_replacement_format_is_invalidated(self):
        self.run_prepare()
        old_scope = digest({"model": "test", "instructions": None, "tools": None})
        with self.compactor._connect() as db:
            db.execute("UPDATE mappings SET scope=?", (old_scope,))
        _, _, meta = self.run_prepare()
        self.assertFalse(meta["overflow_cache_hit"])
        self.assertTrue(meta["overflow_cache_reset"])
        self.assertGreater(meta["overflow_compaction_calls"], 0)

    def test_preserving_user_text_can_safely_prevent_fitting_a_small_budget(self):
        raw = encode(json.loads(make_body([message(i, role="user") for i in range(14)])))
        with self.assertRaises(OverflowFailure) as failure:
            self.run_prepare(raw)
        self.assertEqual(failure.exception.code, "overflow_compaction_no_progress")
        self.assertFalse(self.compactor.path.exists())


class NativeReassemblyTests(unittest.TestCase):
    @staticmethod
    def user(text, **extra):
        return {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": text}], **extra}

    def test_prompt_and_prefix_match_upstream_template_hashes(self):
        # Golden hashes of OpenAI Codex e18a20d6 templates, normalized to LF.
        self.assertEqual(hashlib.sha256(CHECKPOINT_PROMPT.encode()).hexdigest(),
                         "ab0c334d4faca17e3afbb9b16967c1b2fdcc7242a9a0880af57949fa236d6d07")
        self.assertEqual(hashlib.sha256(CHECKPOINT_PREFIX.encode()).hexdigest(),
                         "e9b088e794a6bb9082ac053fcc760bd818d7e720ee4bcdc72c6e480de7b7cb0e")

    def test_summary_is_native_user_input_text_with_exact_prefix_separator(self):
        self.assertEqual(rebuild_prefix([], "Keep reference 7319."), [
            self.user(CHECKPOINT_PREFIX + "\nKeep reference 7319.")])

    def test_recent_plain_user_messages_and_context_keep_original_order(self):
        dev = {"role": "developer", "content": "Permission constraints remain authoritative."}
        first, recent = self.user("First requirement."), self.user("Most recent correction.")
        agents = self.user("# AGENTS.md instructions\n<INSTRUCTIONS>Preserve exact ids.</INSTRUCTIONS>")
        items = [dev, first, message("discarded assistant"), agents, recent]
        result = rebuild_prefix(items, "Summary")
        self.assertEqual(result[:-1], [dev, first, agents, recent])
        self.assertIs(result[1], first)
        self.assertIs(result[-2], recent)

    def test_native_budget_keeps_recent_users_and_head_tail_of_boundary_user(self):
        old, boundary, recent = self.user("discard oldest"), self.user("ABCDEFGHIJKL"), self.user("new!")
        result = rebuild_prefix([old, boundary, recent], "Summary", max_user_tokens=3)
        self.assertEqual(result[:-1], [
            {**boundary, "content": [{"type": "input_text", "text": "ABCD"},
                {"type": "input_text", "text": "…1 tokens truncated…"},
                {"type": "input_text", "text": "IJKL"}]}, recent])

    def test_utf8_and_multiple_text_parts_follow_native_byte_boundaries(self):
        parts = [{"type": "input_text", "text": "中文abc"}, {"type": "input_text", "text": "def汉字"}]
        self.assertEqual(truncate_user_parts(parts, 2), [
            {"type": "input_text", "text": "中"},
            {"type": "input_text", "text": "…3 tokens truncated…"},
            {"type": "input_text", "text": "字"}])

    def test_native_default_retains_twenty_thousand_approximate_tokens(self):
        boundary, recent = self.user("a" * 100_000), self.user("b" * 20_000)
        result = rebuild_prefix([boundary, recent], "Summary")
        self.assertEqual(result[0]["content"][0]["text"], "a" * 30_000)
        self.assertEqual(result[0]["content"][-1]["text"], "a" * 30_000)
        self.assertEqual(result[1], recent)

    def test_repeated_compaction_replaces_old_summary_without_duplicating_it(self):
        user = self.user("Original user request.")
        first = rebuild_prefix([user, message(1)], "Old summary")
        second = rebuild_prefix(first + [message(2)], "New summary")
        self.assertEqual(second, [user, self.user(CHECKPOINT_PREFIX + "\nNew summary")])

    def test_rich_annotated_and_environment_user_context_is_kept_verbatim(self):
        media = {"role": "user", "content": [{"type": "input_image", "image_url": "https://example.invalid/attachment"}]}
        annotated = self.user("Annotated authorization", internal_chat_message_metadata_passthrough={
            "content_item_kinds": ["user.text"]})
        environment = self.user("<environment_context>Current workspace</environment_context>")
        opaque = {"type": "compaction", "encrypted_content": "opaque-state-do-not-prune"}
        items = [media, annotated, environment, opaque]
        self.assertEqual(rebuild_prefix(items, "Summary", max_user_tokens=0)[:-1], items)


class CheckpointTests(unittest.TestCase):
    def test_chunk_boundaries_and_crlf(self):
        parser = CheckpointStream()
        for byte in complete().replace(b"\n", b"\r\n"):
            parser.feed(bytes([byte]))
        self.assertIn("7319", parser.finish())

    def test_incomplete_error_empty_and_tool_output_are_rejected(self):
        samples = [b"data: {\"type\":\"response.created\"}\n\n", b"response protection is unavailable",
                   b"data: {\"type\":\"response.failed\"}\n\n", complete(""),
                   b"data: " + encode({"type": "response.completed", "response": {"status": "completed", "output": [
                       {"type": "function_call", "call_id": "never-execute", "name": "business"}]}}) + b"\n\n"]
        for raw in samples:
            with self.subTest(raw=raw[:40]), self.assertRaises(OverflowFailure):
                parser = CheckpointStream()
                parser.feed(raw)
                parser.finish()

    def test_only_last_assistant_message_is_used_without_rewriting_its_text(self):
        parser = CheckpointStream()
        parser.feed(b"data: " + encode({"type": "response.completed", "response": {
            "status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": "Earlier commentary"},
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "  Final summary "},
                    {"type": "output_text", "text": "7319.\n"}]},
                {"type": "message", "role": "assistant", "content": []}]}}) + b"\n\n")
        self.assertEqual(parser.finish(), "  Final summary 7319.\n")


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        value = json.loads(decode_body(raw, self.headers.get("Content-Encoding", "identity")))
        metadata = value.get("client_metadata", {})
        compact = "compaction" in metadata.get("x-codex-turn-metadata", "")
        self.server.requests.append((len(raw), compact, value))
        if (self.headers.get("X-OpenAI-Internal-Codex-Responses-Lite")
                and value.get("reasoning", {}).get("context") != "all_turns"):
            body = encode({"error": {"message": "Lite requires reasoning.context=all_turns",
                                     "param": "reasoning.context", "code": "unsupported_value"}})
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = (b"data: {\"type\":\"response.created\"}\n\n" if self.server.fail and compact else complete())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OverflowRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.toml"
        self.config.write_text('model="test"\nmodel_auto_compact_token_limit=220000\n', encoding="utf8")
        self.original_config = self.config.read_bytes()
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.up.requests, self.up.fail = [], False
        self.server = InspectorServer(("127.0.0.1", 0), root / "data", self.config,
                                      "http://127.0.0.1:" + str(self.up.server_port) + "/v1")
        self.server.settings.update(packet_size_protection_enabled=True, budget_bytes=4000, request_compression="off")
        for server in (self.up, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for server in (self.server, self.up):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def post(self, items, session="relay-test", headers=None, **fields):
        client = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        path = self.server.endpoint.split(str(self.server.port), 1)[1] + "/responses"
        client.request("POST", path, make_body(items, **fields),
                       {"Content-Type": "application/json", "session_id": session, **(headers or {})})
        response = client.getresponse()
        status, body = response.status, response.read()
        client.close()
        return status, body

    def test_auto_levels_skip_small_rebuilt_summaries_and_keep_uploads_bounded(self):
        self.server.settings.update(request_compression="zstd", auto_zstd_level_enabled=True, zstd_level=9)
        items = [message(i) for i in range(14)]
        for current in (items, items + [message("tail", 30)]):
            status, body = self.post(current, session="auto-overflow")
            self.assertEqual((status, body), (200, complete()))
            for _ in range(100):
                meta = self.server.store.list(limit=1)[0]
                if meta.get("state") == "complete":
                    break
                time.sleep(.005)
            self.assertIsNone(meta["relay_zstd_level"])
            self.assertEqual(meta["relay_content_encoding"], "identity")
            self.assertLess(meta["relay_wire_bytes"], 3600)
        self.assertTrue(any(compact for _, compact, _ in self.up.requests))
        self.assertTrue(all(size <= 4000 for size, _, _ in self.up.requests))

    def test_http_first_reuse_clear_and_native_threshold_unchanged(self):
        items = [message(i) for i in range(14)]
        for sample in (items, items + [message("new", 80)], [message("native-compact", 30)]):
            status, body = self.post(sample)
            self.assertEqual(status, 200)
            self.assertEqual(body, complete())
        self.assertTrue(any(compact for _, compact, _ in self.up.requests))
        self.assertEqual(sum(not compact for _, compact, _ in self.up.requests), 3)
        self.assertTrue(all(size <= 4000 for size, _, _ in self.up.requests))
        self.assertEqual(self.config.read_bytes(), self.original_config)
        with closing(sqlite3.connect(self.server.overflow.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM mappings").fetchone()[0], 0)

    def test_failed_compaction_never_sends_original_turn(self):
        self.up.fail = True
        status, body = self.post([message(i) for i in range(14)])
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "overflow_compaction_incomplete")
        self.assertTrue(self.up.requests)
        self.assertTrue(all(compact for _, compact, _ in self.up.requests))

    def test_lite_http_compaction_keeps_required_context_and_fits_budget(self):
        reasoning = {"effort": "max", "summary": "detailed", "context": "all_turns"}
        status, body = self.post([message(i) for i in range(14)], session="lite-test",
                                 headers={"X-OpenAI-Internal-Codex-Responses-Lite": "1"}, reasoning=reasoning)
        self.assertEqual(status, 200)
        self.assertEqual(body, complete())
        self.assertTrue(any(compact for _, compact, _ in self.up.requests))
        self.assertEqual(sum(not compact for _, compact, _ in self.up.requests), 1)
        self.assertTrue(all(size <= 4000 for size, _, _ in self.up.requests))
        self.assertTrue(all(value["reasoning"] == reasoning for _, _, value in self.up.requests))

    def test_upstream_parameter_error_is_visible_and_original_is_not_sent(self):
        status, body = self.post([message(i) for i in range(14)],
                                 headers={"X-OpenAI-Internal-Codex-Responses-Lite": "1"})
        self.assertEqual(status, 502)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "overflow_compaction_upstream_error")
        self.assertIn("reasoning.context=all_turns", error["message"])
        self.assertTrue(self.up.requests)
        self.assertTrue(all(compact for _, compact, _ in self.up.requests))

    def test_disabled_feature_is_existing_passthrough(self):
        self.server.settings["packet_size_protection_enabled"] = False
        self.assertEqual(self.post([message(i) for i in range(14)])[0], 200)
        self.assertEqual(len(self.up.requests), 1)
        self.assertGreater(self.up.requests[0][0], 4000)
        self.assertFalse(self.server.overflow.path.exists())


if __name__ == "__main__":
    unittest.main()
