import json
import unittest

from packet_inspector.analyzer import inspect_request
from packet_inspector.request_context import (
    IMAGE_ANALYST_PREFIXES, NEXT_MESSAGE_PREFIX, TITLE_REQUEST_PREFIX,
    local_image_path, request_image_paths, temporary_kind_from_metadata, temporary_request_kind,
)


class RequestContextTests(unittest.TestCase):
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
