import json
import unittest

from packet_inspector.analyzer import inspect_request
from packet_inspector.request_context import (
    IMAGE_ANALYST_PREFIXES, NEXT_MESSAGE_PREFIX, TITLE_REQUEST_PREFIX,
    is_delegation_notice, local_image_path, request_image_paths, request_session_context, temporary_kind_from_metadata, temporary_request_kind,
)


class RequestContextTests(unittest.TestCase):
    def delegation_notice(self):
        return {"type": "function_call_output", "id": "fco_00000000-0000-0000-0000-000000000002",
                "output": "<codex_delegation>\n  <source_thread_id>00000000-0000-0000-0000-000000000001</source_thread_id>\n"
                          "  <input>Keep the <instruction>original content & ordering</instruction>.\n已完成交接。</input>\n</codex_delegation>"}

    def test_standalone_delegation_shape_is_recognized_without_parsing_its_instructions(self):
        notice = self.delegation_notice()
        self.assertTrue(is_delegation_notice(notice))
        self.assertTrue(is_delegation_notice({**notice, "output": "\n" + notice["output"] + "\n", "future_metadata": {"x": 1}}))

    def test_real_call_ids_and_unrecognized_outputs_are_not_exempted(self):
        notice = self.delegation_notice()
        cases = [{**notice, "call_id": value} for value in ("real-call", None, "")]
        cases += [{**notice, "id": value} for value in (None, "fco_", "ordinary-item")]
        cases += [{**notice, "type": "custom_tool_call_output"},
                  {key: value for key, value in notice.items() if key != "id"}]
        cases += [{**notice, "output": value} for value in (
            "ordinary tool result", "quoted " + notice["output"], notice["output"] + " unrelated trailing data",
            notice["output"].replace("</codex_delegation>", ""),
            notice["output"].replace("00000000-0000-0000-0000-000000000001", "unconfirmed"),
            {"text": notice["output"]}, None)]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(is_delegation_notice(value))

    def test_notification_items_do_not_inflate_tool_pairing_counts(self):
        body = {"input": [self.delegation_notice(),
                          {"type": "function_call", "call_id": "real-call", "name": "example", "arguments": "{}"},
                          {"type": "function_call_output", "call_id": "real-call", "output": "ok"}]}
        metadata, _ = inspect_request(json.dumps(body).encode(), {}, "/responses")
        self.assertEqual(metadata["pairing"], {"calls": 1, "outputs": 1, "missing_outputs": 0, "orphan_outputs": 0})
        metadata, _ = inspect_request(json.dumps({"input": [{**self.delegation_notice(), "output": "ordinary result"}]}).encode(), {}, "/responses")
        self.assertEqual(metadata["pairing"]["orphan_outputs"], 1)

    def test_specific_purpose_wins_over_generic_subagent_metadata(self):
        for meta, expected in (({"session_kind": "subagent", "request_kind": "image_analysis"}, "image_analysis"),
                               ({"session_kind": "subagent", "input_items": [
                                   {"kind": "message:user", "label": NEXT_MESSAGE_PREFIX[:140]}]}, "next_message_suggestion")):
            self.assertEqual(temporary_kind_from_metadata(meta), expected)
        self.assertEqual(temporary_kind_from_metadata({"session_kind": "subagent"}), "subagent")
        body = {"input": [{"role": "user", "content": IMAGE_ANALYST_PREFIXES[0]}],
                "client_metadata": {"thread_source": "subagent"}}
        self.assertEqual(request_session_context(body)["session_kind"], "image_analysis")

    def test_storage_exposes_one_purpose_for_list_and_detail_and_preserves_unknown_fields(self):
        from pathlib import Path
        import tempfile
        from packet_inspector.storage import Store
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            meta = {"method": "POST", "session_kind": "subagent", "future_field": {"value": "retained"},
                    "input_items": [{"kind": "message:user", "label": TITLE_REQUEST_PREFIX[:140]}]}
            identifier = store.add(meta)
            detail, _ = store.get(identifier)
            summary = store.list()[0]
            for value in (detail, summary):
                self.assertEqual(value["request_purpose"], "title_generation")
                self.assertEqual(value["future_field"], meta["future_field"])

    def test_explicit_fork_metadata_survives_compaction_classification(self):
        parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
        body = {"input": [{"role": "user", "content": "Create a context checkpoint"}],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "thread_source": "thread_description", "request_kind": "compaction",
                    "forked_from_thread_id": parent})}}
        meta, _ = inspect_request(json.dumps(body).encode(), {"Content-Type": "application/json"}, "/responses")
        self.assertEqual(meta["parent_thread_id"], parent)
        self.assertEqual(meta["session_kind"], "session_index")
        self.assertEqual(meta["request_kind"], "compaction")
        self.assertTrue(meta["is_compaction"])

    def test_parent_ids_require_explicit_unambiguous_metadata(self):
        parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
        other = "01a166b7-e4a7-7550-8eaa-06e64a78e986"
        for body in (None, [], {"prompt_cache_key": parent},
                     {"input": [{"role": "user", "content": "parent_thread_id=" + parent}]},
                     {"client_metadata": {"parent_thread_id": "not-a-session", "thread_source": []}},
                     {"client_metadata": {"parent_thread_id": parent}, "metadata": {"parent_session_id": other}}):
            with self.subTest(body=body):
                self.assertNotIn("parent_thread_id", request_session_context(body))
        self.assertEqual(request_session_context({}, {"X-Codex-Turn-Metadata": json.dumps({
            "parent_thread_id": parent})}), {"parent_thread_id": parent})

    def test_only_latest_user_explicit_helper_prompt_sets_purpose(self):
        for prefix, kind in ((NEXT_MESSAGE_PREFIX, "next_message_suggestion"),
                             (TITLE_REQUEST_PREFIX, "title_generation"),
                             (IMAGE_ANALYST_PREFIXES[0], "image_analysis")):
            with self.subTest(kind=kind):
                body = {"input": [{"role": "user", "content": prefix}]}
                self.assertEqual(temporary_request_kind(body), kind)
                body["input"].append({"role": "assistant", "content": "done"})
                self.assertEqual(temporary_request_kind(body), kind)
                body["input"].append({"role": "user", "content": "Continue the actual task"})
                self.assertIsNone(temporary_request_kind(body))
                body["input"][-1]["content"] = "A quote: " + prefix
                self.assertIsNone(temporary_request_kind(body))

    def test_saved_metadata_recognizes_helpers_without_body(self):
        for prefix, kind in ((NEXT_MESSAGE_PREFIX, "next_message_suggestion"),
                             (TITLE_REQUEST_PREFIX, "title_generation")):
            meta = {"input_items": [{"kind": "message:user", "label": prefix[:140]}]}
            self.assertEqual(temporary_kind_from_metadata(meta), kind)
        self.assertEqual(temporary_kind_from_metadata({"image_isolation_worker": True}), "image_analysis")
        self.assertIsNone(temporary_kind_from_metadata({"image_isolation_worker": "true", "request_kind": []}))
        self.assertIsNone(temporary_kind_from_metadata({"request_kind": "generation"}))

    def test_attachment_paths_only_no_image_reads(self):
        windows = r"C:\attachments\example.png"
        body = {"input": [
            {"role": "assistant", "content": '<image path="C:\\ignored.png">'},
            {"type": "function_call_output", "output": '<image path="C:\\ignored.png">'},
            {"role": "user", "content": [
                {"type": "input_text", "text": '<image name="image" path="' + windows + '">\n</image>'},
                {"type": "input_text", "text": windows},
                {"type": "input_image", "image_url": "file:///C:/attachments/a%20b.webp"},
                {"type": "input_image", "image_url": "https://example.org/image.png"},
            ]},
        ]}
        self.assertEqual(request_image_paths(body), [windows, "C:/attachments/a b.webp"])
        for value in ("relative.png", "https://example.org/image.png", "data:image/png;redacted", "C:\\a.png\ncode"):
            self.assertIsNone(local_image_path(value))

    def test_metadata_contains_purpose_and_paths(self):
        body = {"model": "test", "input": [{"role": "user", "content": NEXT_MESSAGE_PREFIX}]}
        meta, _ = inspect_request(json.dumps(body).encode(), {}, "/responses")
        self.assertEqual(meta["request_kind"], "next_message_suggestion")
        self.assertEqual(meta["image_paths"], [])


if __name__ == "__main__":
    unittest.main()
