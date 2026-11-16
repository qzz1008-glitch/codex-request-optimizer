"""Clipboard relocation uses synthetic files; no user images or model calls."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import base64
import hashlib
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
        self.isolation = ImageIsolation(self.root / "data")
        self.cache = ClipboardImageCache(self.isolation.directory, self.isolation._copy_payload,
                                         root=self.temp, max_file_bytes=100, ttl=3600)
        self.isolation.clipboard = self.cache

    def source(self, number=1, content=b"synthetic clipboard attachment"):
        path = self.temp / ("codex-clipboard-" + str(uuid.UUID(int=number)) + ".png")
        path.write_bytes(content)
        return path

    @staticmethod
    def inline(payload):
        return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(payload).decode()}

    def test_clipboard_and_inline_image_use_one_file_and_one_context_path(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic shared attachment"
        source = self.source(content=payload)
        tag = '<image path="' + str(source) + '">'
        body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": tag}, self.inline(payload),
            {"type": "input_text", "text": "</image>"}]}]}
        raw = b' { "untouched":"\\u0061", ' + encode(body)[1:]
        target = None
        for coding, inbound in (("identity", raw), ("gzip", gzip.compress(raw)),
                                ("zstd", zstandard.ZstdCompressor().compress(raw))):
            wire, sent_coding, meta = self.isolation.prepare(inbound, {"Content-Encoding": coding}, "/responses", "zstd")
            decoded = decode_body(wire, sent_coding)
            parts = json.loads(decoded)["input"][0]["content"]
            target = Path(parts[1]["text"])
            self.assertEqual(parts[0]["text"], tag.replace(str(source), str(target)))
            self.assertEqual(meta["image_paths"], [str(target)])
            self.assertEqual(meta["image_path_rewrites"], {str(source): str(target)})
            self.assertEqual(meta["image_isolation_removed"], 1)
            self.assertEqual(list(self.isolation.directory.glob("*.png")), [target])
            self.assertTrue(decoded.startswith(b' { "untouched":"\\u0061", '))
        self.assertEqual(target.parent.name, "images")
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(source.read_bytes(), payload)
        self.assertFalse((self.temp / "CodexRequestOptimizer").exists())

    def test_existing_inline_cache_is_reused_without_another_copy(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic existing attachment"
        existing = self.isolation._store(self.inline(payload)["image_url"], set())
        source = self.source(content=payload)
        self.assertEqual(self.cache.prepare([str(source)]), {str(source): str(existing)})
        self.assertEqual(list(self.isolation.directory.glob("*.png")), [existing])

    def test_inline_image_supplies_the_path_when_the_original_is_missing_or_different(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic inline attachment"
        source = self.source(content=b"different native clipboard encoding")
        tag = '<image path="' + str(source) + '">'
        body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": tag}, self.inline(payload)]}]}
        for original_exists in (True, False):
            if not original_exists:
                source.unlink()
            with self.subTest(original_exists=original_exists):
                wire, coding, meta = self.isolation.prepare(encode(body), {}, "/responses", "off")
                parts = json.loads(decode_body(wire, coding))["input"][0]["content"]
                target = Path(parts[1]["text"])
                self.assertEqual(parts[0]["text"], tag.replace(str(source), str(target)))
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(meta["image_paths"], [str(target)])
                self.assertEqual(list(self.isolation.directory.glob("*.png")), [target])
        restarted = ImageIsolation(self.root / "data")
        restarted.clipboard.root = self.temp.resolve()
        self.assertEqual(restarted.clipboard.prepare([str(source)]), {str(source): str(target)})
        self.assertFalse((self.temp / "CodexRequestOptimizer").exists())

    def test_inline_save_does_not_prune_referenced_permanent_paths(self):
        source = self.source(content=b"previous image")
        previous = Path(self.cache.prepare([str(source)])[str(source)])
        source.unlink()
        os.utime(previous, (1, 1))
        payload = b"\x89PNG\r\n\x1a\nsynthetic new attachment"
        body = {"input": [{"role": "user", "content": str(source)},
                          {"role": "user", "content": [self.inline(payload)]}]}
        wire, coding, meta = self.isolation.prepare(encode(body), {}, "/responses", "off")
        self.assertEqual(json.loads(decode_body(wire, coding))["input"][0]["content"], str(previous))
        self.assertTrue(previous.is_file())
        self.assertIn(str(previous), meta["image_paths"])

    def test_replaced_alias_is_not_included_in_display_paths(self):
        source = self.source(content=b"different clipboard encoding")
        old = Path(self.cache.prepare([str(source)])[str(source)])
        payload = b"\x89PNG\r\n\x1a\nsynthetic inline encoding"
        tag = '<image path="' + str(source) + '">'
        body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": tag}, self.inline(payload)]}]}
        wire, coding, meta = self.isolation.prepare(encode(body), {}, "/responses", "off")
        parts = json.loads(decode_body(wire, coding))["input"][0]["content"]
        target = Path(parts[1]["text"])
        self.assertNotEqual(target, old)
        self.assertEqual(parts[0]["text"], tag.replace(str(source), str(target)))
        self.assertEqual(meta["image_paths"], [str(target)])

    def test_aliases_survive_restart_after_original_is_removed(self):
        source = self.source()
        expected = self.cache.prepare([str(source)])
        source.unlink()
        restarted = ImageIsolation(self.root / "data")
        restarted.clipboard.root = self.temp.resolve()
        self.assertEqual(restarted.clipboard.prepare([str(source)]), expected)
        self.assertEqual(len(list(restarted.directory.glob("*.png"))), 1)

    def test_two_images_are_not_merged_and_share_one_directory(self):
        payloads = [b"\x89PNG\r\n\x1a\nfirst", b"\x89PNG\r\n\x1a\nsecond"]
        sources = [self.source(i + 1, payload) for i, payload in enumerate(payloads)]
        body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": '<image path="' + str(path) + '">'}, self.inline(payload)]}
            for path, payload in zip(sources, payloads)]}
        wire, coding, meta = self.isolation.prepare(encode(body), {}, "/responses", "off")
        rewritten = json.loads(decode_body(wire, coding))
        targets = [Path(item["content"][1]["text"]) for item in rewritten["input"]]
        self.assertEqual(len(set(targets)), 2)
        self.assertEqual(len(list(self.isolation.directory.glob("*.png"))), 2)
        self.assertEqual(meta["image_paths"], sorted(map(str, targets)))
        self.assertEqual([path.read_bytes() for path in targets], payloads)

    def test_old_cache_reference_stays_on_its_original_path(self):
        previous = self.root / "data" / "image-attachments"
        previous.mkdir(parents=True)
        old = previous / ("i-" + "f" * 30 + ".png")
        old.write_bytes(b"synthetic old attachment")
        self.cache.previous_directories.add(previous.resolve())
        raw = encode({"input": [{"role": "user", "content": str(old)}]})
        self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))
        self.assertTrue(old.is_file())
        self.assertEqual(list(self.isolation.directory.glob("*.png")), [])

    def test_alias_metadata_is_bounded_and_never_uses_outside_paths(self):
        with patch("packet_inspector.clipboard_images.MAX_ALIASES", 2):
            for i in range(3):
                self.cache.prepare([str(self.source(i + 1, str(i).encode()))])
        saved = json.loads(self.cache.aliases_path.read_text(encoding="utf8"))
        self.assertEqual(len(saved), 2)
        source = self.source(10)
        saved[os.path.normcase(str(source.resolve()))] = {"name": "../outside.png", "used": 1e12}
        self.cache.aliases_path.write_text(json.dumps(saved), encoding="utf8")
        source.unlink()
        self.assertEqual(self.cache.prepare([str(source)]), {})

    def test_historical_permanent_cache_is_the_only_context_path(self):
        payload = b"\x89PNG\r\n\x1a\nsynthetic historical image"
        source = self.source(content=payload)
        previous = self.root / "data" / "image-attachments"
        previous.mkdir(parents=True)
        saved = previous.resolve() / ("i-" + hashlib.sha256(payload).hexdigest()[:30] + ".png")
        saved.write_bytes(payload)
        self.cache.previous_directories.add(previous.resolve())
        for path in (source, saved):
            os.utime(path, (1, 1))
        tag = '<image path="' + str(source) + '">'
        body = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": tag}, self.inline(payload)]}]}
        expected = encode({"input": [{"role": "user", "content": [
            {"type": "input_text", "text": tag.replace(str(source), str(saved))},
            {"type": "input_text", "text": str(saved)}]}]})
        for original_exists in (True, False):
            if not original_exists:
                source.unlink()
            wire, coding, meta = self.isolation.prepare(encode(body), {}, "/responses", "off")
            self.assertEqual(decode_body(wire, coding), expected)
            self.assertEqual(meta["image_paths"], [str(saved)])
            self.assertEqual(list(self.isolation.directory.glob("*.png")), [])
        os.utime(saved, (1, 1))
        self.cache.prepare([str(self.source(2, b"new attachment"))])
        self.assertFalse(saved.exists(), "Unreferenced expired files in the old permanent directory are reclaimed too")
        self.assertFalse((self.temp / "CodexRequestOptimizer").exists())

    def test_concurrent_isolation_instances_reuse_the_same_cache(self):
        source = self.source()
        second = ImageIsolation(self.root / "data")
        second.clipboard.root = self.temp.resolve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cache: cache.prepare([str(source)]), [self.cache, second.clipboard]))
        self.assertEqual(results[0], results[1])
        self.assertIn(str(source), results[0])
        self.assertEqual(len(list(self.isolation.directory.glob("*.png"))), 1)

    def test_modified_original_does_not_reuse_a_stale_alias(self):
        source = self.source(content=b"old")
        old = Path(self.cache.prepare([str(source)])[str(source)])
        source.write_bytes(b"new")
        target = Path(self.cache.prepare([str(source)])[str(source)])
        self.assertNotEqual(target, old)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(old.read_bytes(), b"old")

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
        target = Path(metrics["image_paths"][0])
        self.assertEqual(target.parent, self.isolation.directory)
        self.assertFalse((self.temp / "CodexRequestOptimizer").exists())
        self.assertEqual(target.stem, "i-" + hashlib.sha256(source.read_bytes()).hexdigest()[:30])
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
                target = Path(body["input"][0]["content"])
                self.assertEqual(target.parent, self.cache.directory)
                self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_content_deduplication_and_short_names(self):
        sources = [self.source(number, str(number).encode()) for number in (0, 1, (1 << 128) - 1)]
        copied = self.cache.prepare([str(path) for path in sources])
        names = [Path(copied[str(path)]).name for path in sources]
        self.assertEqual(names, ["i-" + hashlib.sha256(path.read_bytes()).hexdigest()[:30] + ".png" for path in sources])
        self.assertEqual(len(set(names)), len(sources))
        self.assertTrue(all(len(Path(name).stem) <= 32 for name in names))
        duplicate = self.source(2, sources[0].read_bytes())
        self.assertEqual(self.cache.prepare([str(duplicate)])[str(duplicate)], copied[str(sources[0])])

    def test_removed_temporary_cache_is_not_an_image_source(self):
        source = self.source()
        retired = self.temp / "CodexRequestOptimizer"
        retired.mkdir()
        legacy = retired / source.name
        legacy.write_bytes(source.read_bytes())
        source.unlink()
        self.assertEqual(self.cache.prepare([str(source), str(legacy)]), {})
        self.assertEqual(list(self.isolation.directory.glob("*.png")), [])

    def test_obsolete_alias_to_temporary_copy_is_discarded(self):
        source = self.source()
        retired = self.temp / "CodexRequestOptimizer"
        retired.mkdir()
        legacy = retired / (uuid.uuid4().hex + ".png")
        legacy.write_bytes(source.read_bytes())
        self.isolation.directory.mkdir(parents=True)
        key = os.path.normcase(str(source.resolve()))
        self.cache.aliases_path.write_text(json.dumps({key: {
            "name": legacy.name, "path": str(legacy), "used": 1e12}}), encoding="utf8")
        target = Path(self.cache.prepare([str(source)])[str(source)])
        self.assertEqual(target.parent, self.isolation.directory)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(list(self.isolation.directory.glob("*.png")), [target])
        saved = json.loads(self.cache.aliases_path.read_text(encoding="utf8"))
        self.assertEqual(saved[key]["path"], str(target))

    def test_cached_copies_survive_original_removal_and_protect_all_current_paths(self):
        sources = [self.source(1, b"first"), self.source(2, b"second")]
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
        changed = Path(self.cache.prepare([str(source)])[str(source)])
        self.assertNotEqual(changed, target)
        self.assertEqual(changed.read_bytes(), b"changed original")
        self.assertEqual(target.read_bytes(), b"first")

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
        legacy = self.cache.directory / ("a" * 32 + ".png")
        legacy.write_bytes(b"old copy")
        os.utime(legacy, (1, 1))
        other = self.cache.directory / "keep-me.png"
        other.write_bytes(b"keep")
        second = self.source(2, b"different")
        self.assertTrue(self.cache.prepare([str(second)]))
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertTrue(first.exists())

    def test_budget_limits_copies_and_failed_writes_leave_no_staging(self):
        with patch("packet_inspector.image_isolation.MAX_ATTACHMENTS_BYTES", 30):
            self.assertTrue(self.cache.prepare([str(self.source(1, b"a" * 20))]))
            self.assertEqual(self.cache.prepare([str(self.source(2, b"b" * 20))]), {})
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.cache.prepare([str(self.source(3, b"c" * 20))]), {})
        self.assertEqual(list(self.cache.directory.glob("*.new")), [])

    def test_concurrent_cache_instances_share_atomic_copies(self):
        source = self.source()
        second = ClipboardImageCache(self.isolation.directory, self.isolation._copy_payload, root=self.temp)
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
