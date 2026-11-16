"""Image cache retention uses only synthetic files and no model calls."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from packet_inspector.analyzer import encode
from packet_inspector.image_isolation import ATTACHMENT_TTL, CLEANUP_INTERVAL, ImageIsolation, ImageIsolationFailure
from packet_inspector.locking import file_lock


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.cache = ImageIsolation(self.root / "data")
        self.busy = threading.Event()

    def owned(self, number=1, *, old=False, payload=b"synthetic cache"):
        directory = self.cache.previous_directory if old else self.cache.directory
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("i-" + f"{number:030x}" + ".png")
        path.write_bytes(payload)
        return path

    @staticmethod
    def age(path, seconds=ATTACHMENT_TTL + 1):
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    @staticmethod
    def request(payload):
        return {"input": [{"role": "user", "content": [{"type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(payload).decode()}]}]}

    def test_empty_maintenance_does_not_create_cache_files(self):
        self.assertEqual(self.cache.maintenance(self.busy)["state"], "absent")
        self.assertFalse((self.root / "data").exists())

    def test_new_and_old_directories_expire_together_without_new_requests(self):
        expired = [self.owned(1), self.owned(2, old=True)]
        recent = [self.owned(3), self.owned(4, old=True)]
        for path in expired:
            self.age(path)
        result = self.cache.maintenance(self.busy)
        self.assertEqual(result["expired"], 2)
        self.assertEqual(result["reclaimed_bytes"], sum(len(b"synthetic cache") for _ in expired))
        self.assertTrue(all(not path.exists() for path in expired))
        self.assertTrue(all(path.exists() for path in recent))

    def test_periodic_cleanup_does_not_scan_on_every_maintenance_tick(self):
        self.owned()
        with patch("packet_inspector.image_isolation.time.monotonic", return_value=1000):
            self.cache.maintenance(self.busy)
        stale = self.owned(2)
        self.age(stale)
        with patch("packet_inspector.image_isolation.time.monotonic", return_value=1001):
            self.cache.maintenance(self.busy)
        self.assertTrue(stale.exists())
        with patch("packet_inspector.image_isolation.time.monotonic", return_value=1000 + CLEANUP_INTERVAL):
            self.cache.maintenance(self.busy)
        self.assertFalse(stale.exists())

    def test_busy_requests_defer_even_forced_cleanup(self):
        path = self.owned(old=True)
        self.age(path)
        self.busy.set()
        self.cache.maintenance(self.busy, force=True)
        self.assertTrue(path.exists())
        self.assertEqual(self.cache.next_cleanup, 0)
        self.busy.clear()
        self.cache.maintenance(self.busy)
        self.assertFalse(path.exists())

    def test_arriving_request_interrupts_cleanup_between_files(self):
        paths = [self.owned(1), self.owned(2)]
        for path in paths:
            self.age(path)
        unlink = Path.unlink
        def remove(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            self.busy.set()
        with patch.object(Path, "unlink", remove):
            result = self.cache.maintenance(self.busy)
        self.assertEqual(result["state"], "deferred")
        self.assertEqual(sum(path.exists() for path in paths), 1)
        self.busy.clear()
        self.cache.maintenance(self.busy, force=True)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_storage_lock_contention_defers_instead_of_waiting(self):
        path = self.owned()
        self.age(path)
        with file_lock(self.cache.lock_path):
            self.cache.maintenance(self.busy)
        self.assertTrue(path.exists())
        self.cache.maintenance(self.busy, force=True)
        self.assertFalse(path.exists())

    def test_only_owned_files_are_removed_and_staging_uses_one_hour(self):
        self.owned()
        stale = self.cache.directory / ("a" * 32 + ".new")
        fresh = self.cache.directory / ("b" * 32 + ".new")
        foreign = self.cache.directory / "keep-my-photo.png"
        nested = self.cache.directory / "nested" / ("c" * 32 + ".png")
        nested.parent.mkdir()
        for path in (stale, fresh, foreign, nested):
            path.write_bytes(b"synthetic")
        for path in (stale, foreign, nested):
            self.age(path, 4000 if path == stale else ATTACHMENT_TTL + 1)
        self.cache.maintenance(self.busy)
        self.assertFalse(stale.exists())
        self.assertTrue(all(path.exists() for path in (fresh, foreign, nested)))

    def test_expired_aliases_and_aliases_to_removed_files_are_reclaimed(self):
        source = self.root / "codex-clipboard-00000000-0000-0000-0000-000000000001.png"
        source.write_bytes(b"synthetic original")
        self.cache.clipboard.root = self.root
        target = Path(self.cache.clipboard.prepare([str(source)])[str(source)])
        source.unlink()
        self.age(target)
        self.cache.maintenance(self.busy)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(self.cache.clipboard.aliases_path.read_text(encoding="utf8")), {})

    def test_expired_alias_metadata_is_reclaimed_even_when_the_file_is_recent(self):
        source = self.root / "codex-clipboard-00000000-0000-0000-0000-000000000001.png"
        source.write_bytes(b"synthetic original")
        self.cache.clipboard.root = self.root
        target = Path(self.cache.clipboard.prepare([str(source)])[str(source)])
        aliases = json.loads(self.cache.clipboard.aliases_path.read_text(encoding="utf8"))
        for record in aliases.values():
            record["used"] = time.time() - ATTACHMENT_TTL - 1
        self.cache.clipboard.aliases_path.write_text(json.dumps(aliases), encoding="utf8")
        self.cache.maintenance(self.busy)
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(self.cache.clipboard.aliases_path.read_text(encoding="utf8")), {})

    def test_shared_budget_never_evicts_recent_files_to_make_room(self):
        old = self.owned(old=True, payload=b"a" * 20)
        with patch("packet_inspector.image_isolation.MAX_ATTACHMENTS_BYTES", 30):
            with self.assertRaises(ImageIsolationFailure) as caught:
                self.cache._store_payload(b"b" * 20, ".png", set())
            self.assertEqual(caught.exception.code, "image_attachment_storage_full")
            self.assertTrue(old.exists())
            self.age(old)
            saved = self.cache._store_payload(b"b" * 20, ".png", set())
        self.assertFalse(old.exists())
        self.assertTrue(saved.exists())

    def test_image_analysis_refreshes_only_managed_sources(self):
        for path in (self.owned(1), self.owned(2, old=True), self.root / "user-photo.png"):
            if not path.exists():
                path.write_bytes(b"synthetic user file")
            self.age(path)
            before = path.stat().st_mtime
            body = {"client_metadata": {"codex_inspector_request_kind": "image_analysis",
                                       "codex_inspector_image_source_path": str(path)}}
            self.cache.observe_request(encode(body), {})
            if path.parent == self.root:
                self.assertEqual(path.stat().st_mtime, before)
            else:
                self.assertGreater(path.stat().st_mtime, before)
        self.cache.maintenance(self.busy)
        self.assertEqual(self.cache.last_cleanup["expired"], 0)

    def test_parallel_preparation_protects_other_requests_and_releases_after_completion(self):
        path = self.owned(old=True)
        entered, release = threading.Event(), threading.Event()
        payload = b"\x89PNG\r\n\x1a\nsynthetic first"
        first = self.request(payload)
        first["input"].insert(0, {"role": "user", "content": str(path)})
        original = self.cache._store
        def store(url, protected):
            if base64.b64encode(payload).decode() in url:
                self.age(path)
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("parallel preparation did not finish")
            return original(url, protected)
        with patch.object(self.cache, "_store", store), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.cache.prepare, encode(first), {}, "/responses", "off")
            try:
                self.assertTrue(entered.wait(3))
                second = self.request(b"\x89PNG\r\n\x1a\nsynthetic second")
                self.cache.prepare(encode(second), {}, "/responses", "off")
                self.assertTrue(path.exists())
                self.cache.maintenance(self.busy, force=True)
                self.assertTrue(path.exists())
            finally:
                release.set()
            pending.result(timeout=3)
        self.assertEqual(self.cache.active_references, {})
        self.age(path)
        self.cache.maintenance(self.busy, force=True)
        self.assertFalse(path.exists())

    def test_preparation_failure_does_not_leak_reference_protection(self):
        with self.assertRaises(ImageIsolationFailure):
            self.cache.prepare(b"invalid", {}, "/responses", "off")
        self.assertEqual(self.cache.active_references, {})

    def test_invalid_observation_cannot_fail_a_relay_request(self):
        self.cache.observe_request(b"invalid zstd stream", {"Content-Encoding": "zstd"})
        self.assertFalse((self.root / "data").exists())

    def test_unlink_failure_is_reported_without_blocking_new_images(self):
        path = self.owned(old=True)
        self.age(path)
        unlink = Path.unlink
        def remove(target, *args, **kwargs):
            if target == path:
                raise PermissionError("synthetic locked file")
            return unlink(target, *args, **kwargs)
        with patch.object(Path, "unlink", remove):
            result = self.cache.maintenance(self.busy)
            saved = self.cache._store_payload(b"synthetic new image", ".png", set())
        self.assertEqual(result["errors"], 1)
        self.assertTrue(path.exists())
        self.assertTrue(saved.exists())


if __name__ == "__main__":
    unittest.main()
