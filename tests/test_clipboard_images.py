"""Clipboard relocation uses synthetic files; no user images or model calls."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import zstandard

from packet_inspector.analyzer import decode_body, encode
from packet_inspector.clipboard_images import ClipboardImageCache, rewrite_clipboard_text
from packet_inspector.image_isolation import ImageIsolation


class ClipboardImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.cache = ClipboardImageCache(root=self.temp, max_file_bytes=100, max_bytes=400, ttl=3600)
        self.isolation = ImageIsolation(self.root / "data")
        self.isolation.clipboard = self.cache

    def source(self, number=1, content=b"synthetic clipboard attachment"):
        path = self.temp / ("codex-clipboard-" + str(uuid.UUID(int=number)) + ".png")
        path.write_bytes(content)
        return path

    def test_rewrites_user_image_references_and_preserves_originals_and_other_bytes(self):
        source = self.source()
        tag = '<image name="Image #1" path="' + str(source) + '">\n</image>'
        body = {"model": "test", "input": [
            {"role": "developer", "content": tag},
            {"role": "assistant", "content": tag},
            {"type": "function_call_output", "output": tag},
            {"role": "user", "content": [{"type": "input_text", "text": tag}]},
            {"role": "user", "content": "  " + str(source) + "  "}]}
        raw = b' { "extra" : "\\u0061", "input_body":true, ' + encode(body)[1:]
        wire, coding, metrics = self.isolation.prepare(raw, {}, "/responses", "off")
        result = json.loads(decode_body(wire, coding))
        target = self.cache.directory / source.name
        self.assertEqual(target.parent, (self.temp / "CodexRequestOptimizer").resolve())
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertTrue(source.is_file())
        self.assertEqual(result["input"][:3], body["input"][:3])
        self.assertEqual(result["input"][3]["content"][0]["text"], tag.replace(str(source), str(target)))
        self.assertEqual(result["input"][4]["content"], "  " + str(target) + "  ")
        self.assertTrue(wire.startswith(b' { "extra" : "\\u0061", "input_body":true, '))
        self.assertEqual(metrics["image_paths"], [str(target)])
        self.assertEqual(metrics["image_path_rewrites"], {str(source): str(target)})

    def test_rewrites_compressed_requests_and_file_urls(self):
        source = self.source()
        raw = encode({"input": [{"role": "user", "content": source.as_uri()}]})
        for coding, data in (("gzip", gzip.compress(raw)), ("zstd", zstandard.ZstdCompressor().compress(raw))):
            with self.subTest(coding=coding):
                wire, output_coding, _ = self.isolation.prepare(data, {"Content-Encoding": coding}, "/responses/compact", "zstd")
                body = json.loads(decode_body(wire, output_coding))
                self.assertEqual(body["input"][0]["content"], str(self.cache.directory / source.name))

    def test_cached_copies_survive_original_removal_and_protect_all_current_paths(self):
        sources = [self.source(1), self.source(2)]
        copied = self.cache.prepare([str(path) for path in sources])
        for source in sources:
            source.unlink()
            os.utime(copied[str(source)], (1, 1))
        reused = self.cache.prepare([str(path) for path in sources])
        self.assertEqual(reused, copied)
        self.assertTrue(all(Path(value).is_file() for value in reused.values()))
        target = next(iter(reused.values()))
        self.assertEqual(self.cache.prepare([target]), {target: target})

    def test_only_our_clipboard_names_under_temp_are_copied(self):
        outside = self.root / "other"
        outside.mkdir()
        source = self.source()
        foreign = outside / source.name
        foreign.write_bytes(b"foreign")
        user_file = self.temp / "photo.png"
        user_file.write_bytes(b"user file")
        self.assertEqual(self.cache.prepare([str(foreign), str(user_file), str(self.temp / "missing.png")]), {})
        self.assertFalse(self.cache.directory.exists())

    def test_changed_original_refreshes_the_cached_copy(self):
        source = self.source(content=b"first")
        target = Path(self.cache.prepare([str(source)])[str(source)])
        source.write_bytes(b"changed original")
        self.assertEqual(self.cache.prepare([str(source)]), {str(source): str(target)})
        self.assertEqual(target.read_bytes(), b"changed original")

    def test_missing_oversize_and_unwritable_files_keep_original_request(self):
        source = self.source(content=b"x" * 101)
        raw = encode({"input": [{"role": "user", "content": str(source)}]})
        self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))
        source.write_bytes(b"small")
        with patch("packet_inspector.clipboard_images.file_lock", side_effect=PermissionError):
            self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))
        source.unlink()
        self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))

    def test_expired_owned_copies_and_staging_are_cleaned_without_touching_other_files(self):
        first = self.source(1)
        target = Path(self.cache.prepare([str(first)])[str(first)])
        os.utime(target, (1, 1))
        staging = self.cache.directory / (".clipboard-" + "a" * 32 + ".new")
        staging.write_bytes(b"stale")
        os.utime(staging, (1, 1))
        other = self.cache.directory / "keep-me.png"
        other.write_bytes(b"keep")
        second = self.source(2)
        self.assertTrue(self.cache.prepare([str(second)]))
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertTrue(first.exists())

    def test_budget_limits_copies_and_failed_writes_leave_no_staging(self):
        self.cache.max_bytes = 30
        self.assertTrue(self.cache.prepare([str(self.source(1, b"a" * 20))]))
        self.assertEqual(self.cache.prepare([str(self.source(2, b"b" * 20))]), {})
        self.cache.max_bytes = 100
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.cache.prepare([str(self.source(3, b"c" * 20))]), {})
        self.assertEqual(list(self.cache.directory.glob("*.new")), [])

    def test_concurrent_cache_instances_share_atomic_copies(self):
        source = self.source()
        second = ClipboardImageCache(root=self.temp)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cache: cache.prepare([str(source)]), [self.cache, second]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(Path(results[0][str(source)]).read_bytes(), source.read_bytes())
        self.assertEqual(list(self.cache.directory.glob("*.new")), [])

    def test_rewrite_is_limited_to_exact_paths_and_image_attributes(self):
        source = str(self.source())
        copied = {source: str(self.cache.directory / Path(source).name)}
        self.assertEqual(rewrite_clipboard_text("Explain " + source, copied), "Explain " + source)
        self.assertEqual(rewrite_clipboard_text("ordinary text", copied), "ordinary text")


if __name__ == "__main__":
    unittest.main()
