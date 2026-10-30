"""Image endpoint classification, lossless encoding, and history migration."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import zstandard

from packet_inspector.analyzer import CLASSIFICATION_VERSION, inspect_request
from packet_inspector.compression_policy import is_main_request
from packet_inspector.storage import Store
from packet_inspector.transport import prepare_request_info


class ImageTransportTests(unittest.TestCase):
    def test_json_image_endpoints_preserve_every_byte_at_the_requested_level(self):
        raw = b' {"model":"gpt-image-2","prompt":"' + b'unchanged content '*2000 + b'","images":[]}\n'
        for path in ("/images/edits", "/v1/images/generations/?test=1"):
            for level in (1, 3, 16, 22):
                with self.subTest(path=path, level=level):
                    prepared = prepare_request_info(raw, {"Content-Type":"application/json; charset=utf-8"},
                                                    "POST", path, "zstd", level)
                    self.assertEqual((prepared.encoding, prepared.zstd_level), ("zstd", level))
                    self.assertEqual(prepared.body, zstandard.ZstdCompressor(level=level).compress(raw))
                    self.assertEqual(zstandard.ZstdDecompressor().decompress(prepared.body), raw)

    def test_multipart_unsupported_encodings_and_other_paths_are_unchanged(self):
        raw = b'unchanged synthetic body '*1000
        cases = [
            ("POST", "/images/edits", "off", "application/json", "identity"),
            ("GET", "/images/edits", "zstd", "application/json", "identity"),
            ("POST", "/images/edits", "zstd", "multipart/form-data; boundary=test", "identity"),
            ("POST", "/images/edits", "zstd", "", "identity"),
            ("POST", "/images/edits", "zstd", "application/json", "gzip"),
            ("POST", "/images/variations", "zstd", "application/json", "identity"),
            ("POST", "/images/edits/extra", "zstd", "application/json", "identity"),
        ]
        for method, path, mode, content_type, encoding in cases:
            with self.subTest(method=method, path=path, content_type=content_type, encoding=encoding):
                result = prepare_request_info(raw, {"Content-Type":content_type, "Content-Encoding":encoding},
                                              method, path, mode)
                self.assertEqual((result.body, result.encoding, result.zstd_level), (raw, encoding, None))

    def test_small_automatic_image_requests_stay_original_and_existing_zstd_is_not_double_encoded(self):
        raw = b'{"prompt":"' + b'synthetic '*1000 + b'"}'
        result = prepare_request_info(raw, {"content-type":"application/json"}, "POST", "/images/edits", "zstd", 16,
                                      auto_zstd_level=True, budget_bytes=1_000_000)
        self.assertEqual((result.body, result.encoding, result.zstd_level), (raw, "identity", None))
        original = zstandard.ZstdCompressor(level=1).compress(raw*200)
        result = prepare_request_info(original, {"content-type":"application/json", "content-encoding":"zstd"},
                                      "POST", "/images/edits", "zstd", 16)
        self.assertEqual(result.encoding, "zstd")
        self.assertLessEqual(len(result.body), len(original))
        self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw*200)


class ImageClassificationTests(unittest.TestCase):
    def test_image_endpoint_overrides_unrelated_turn_and_compaction_metadata(self):
        body = {"model":"gpt-image-2", "input":[{"type":"compaction_trigger"}]}
        for path, kind in (("/images/edits", "image_edit"), ("/backend-api/codex/images/generations/?x=1", "image_generation")):
            meta, _ = inspect_request(json.dumps(body).encode(), {"x-codex-turn-metadata":'{"request_kind":"compaction"}'}, path)
            self.assertEqual(meta["request_kind"], kind)
            self.assertFalse(meta["is_compaction"])
            self.assertFalse(is_main_request(meta))
            self.assertFalse(is_main_request({"path":path}))

    def test_legacy_image_history_and_evicted_details_are_reclassified_without_reading_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            ids = {}
            blobs = {}
            for path, kind in (("/images/edits", "image_edit"), ("/images/generations/?x=1", "image_generation")):
                for retained in (False, True):
                    meta, content = inspect_request(b'{"model":"gpt-image-2","prompt":"synthetic"}', {}, path)
                    meta.update(method="POST", path=path, classification_version=2, status=400)
                    meta.pop("request_kind")
                    pid = store.add(meta, content if retained else None)
                    store.update(pid, state="complete", outbound_wire_bytes=1234)
                    ids[pid] = kind
                    with store.connect() as connection:
                        blobs[pid] = connection.execute("SELECT content FROM packets WHERE id=?", (pid,)).fetchone()[0]
            self.assertEqual(len(store.list(kind="generation")), 4)
            with patch("packet_inspector.storage.zlib.decompress", side_effect=AssertionError("Image bodies must not be read")):
                self.assertEqual(store.reclassify_pending(), 4)
            self.assertFalse(store.list(kind="generation"))
            for pid, kind in ids.items():
                saved, _ = store.get(pid)
                self.assertEqual((saved["request_kind"], saved["classification_version"]), (kind, CLASSIFICATION_VERSION))
                self.assertEqual((saved["status"], saved["outbound_wire_bytes"]), (400, 1234))
                self.assertIn(pid, [row["id"] for row in store.list(kind=kind)])
                with store.connect() as connection:
                    self.assertEqual(connection.execute("SELECT content FROM packets WHERE id=?", (pid,)).fetchone()[0], blobs[pid])
            self.assertEqual(set(store.filter_options()["kinds"]), {"image_edit", "image_generation"})
            self.assertEqual(len(store.list(sort="kind", direction="asc")), 4)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI behavior checks")
    def test_ui_classifies_old_and_new_image_records_and_offers_matching_filters(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("request_kinds.cjs"))], capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == "__main__":
    unittest.main()
