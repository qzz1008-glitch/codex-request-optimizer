"""Cache retention, active-request protection, and SQLite reclamation tests."""
from contextlib import closing
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from packet_inspector.analyzer import encode
from packet_inspector.maintenance import SQLiteReclaimer
from packet_inspector.overflow import OverflowCompactor
from packet_inspector.server import InspectorServer
from test_overflow_compaction import complete, make_body, message


class CacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = OverflowCompactor(self.root)

    def save(self, key, text="summary", cache=None):
        (cache or self.cache)._save(key, "scope", 1, [message(1)], [{"role": "user", "content": text}])

    def keys(self, cache=None):
        with closing(sqlite3.connect((cache or self.cache).path)) as db:
            return {r[0] for r in db.execute("SELECT key FROM mappings")}

    def age(self, key, seconds, cache=None):
        with (cache or self.cache)._connect() as db:
            db.execute("UPDATE mappings SET updated=? WHERE key=?", (time.time() - seconds, key))

    def test_no_cache_file_is_created_by_idle_maintenance(self):
        self.cache.maintenance(force=True)
        self.assertFalse(self.cache.path.exists())

    def test_expired_mapping_is_not_reused_before_background_sweep(self):
        self.save("old")
        self.age("old", 8 * 86400)
        self.assertIsNone(self.cache._read("old"))
        self.assertFalse(self.keys())

    def test_last_use_refreshes_expiration_without_changing_mapping(self):
        self.save("used")
        self.age("used", 6 * 86400)
        with patch("packet_inspector.overflow.time.time", return_value=2_000_000_000):
            # Put the old timestamp inside this clock's retention window.
            with self.cache._connect() as db:
                db.execute("UPDATE mappings SET updated=?", (2_000_000_000 - 6 * 86400,))
            value = self.cache._read("used")
            self.assertEqual(value["output"][0]["content"], "summary")
        with closing(sqlite3.connect(self.cache.path)) as db:
            self.assertEqual(db.execute("SELECT updated FROM mappings").fetchone()[0], 2_000_000_000)

    def test_startup_sweep_expires_old_entries_without_new_chat_requests(self):
        self.save("old")
        self.save("fresh")
        self.age("old", 8 * 86400)
        restarted = OverflowCompactor(self.root)
        result = restarted.maintenance(force=True)
        self.assertEqual(self.keys(restarted), {"fresh"})
        self.assertEqual(result["expired"], 1)

    def test_lru_count_cap_respects_recent_cache_reads(self):
        cache = OverflowCompactor(self.root, max_entries=2)
        for index, key in enumerate(("one", "two")):
            self.save(key, cache=cache)
            self.age(key, 10 - index, cache)
        cache._read("one")
        self.save("three", cache=cache)
        self.assertEqual(self.keys(cache), {"one", "three"})

    def test_byte_cap_prunes_old_entries(self):
        cache = OverflowCompactor(self.root, max_cache_bytes=600)
        self.save("one", "a" * 400, cache)
        self.save("two", "b" * 400, cache)
        self.assertEqual(self.keys(cache), {"two"})
        self.assertLessEqual(cache.cache_status()["bytes"], 600)

    def test_active_mappings_survive_expiry_and_capacity_pressure(self):
        cache = OverflowCompactor(self.root, max_entries=1)
        with cache.request_scope() as first, cache.request_scope() as second:
            cache._protect("one", first)
            self.save("one", cache=cache)
            cache._protect("two", second)
            self.save("two", cache=cache)
            self.age("one", 8 * 86400, cache)
            cache.maintenance(force=True)
            self.assertEqual(self.keys(cache), {"one", "two"})
        cache.maintenance(force=True)
        self.assertEqual(self.keys(cache), {"two"})
        self.assertFalse(cache.busy.is_set())

    def test_shared_key_remains_protected_until_last_request_exits(self):
        self.save("shared")
        with self.cache.request_scope() as first:
            self.cache._protect("shared", first)
            self.cache._protect("shared", first)
            with self.cache.request_scope() as second:
                self.cache._protect("shared", second)
                self.assertEqual(self.cache.active_keys["shared"], 2)
            self.assertEqual(self.cache.active_keys["shared"], 1)
        self.assertFalse(self.cache.active_keys)

    def test_exception_releases_request_protection(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with self.cache.request_scope() as scope:
                self.cache._protect("failed", scope)
                raise RuntimeError("synthetic")
        self.assertFalse(self.cache.busy.is_set())
        self.assertFalse(self.cache.active_keys)

    def test_deletion_does_not_leave_plaintext_in_freed_summary_pages(self):
        marker = "cache-private-reference-8319-"
        self.save("private", marker * 1000)
        self.assertTrue(marker.encode() in self.cache.path.read_bytes())
        self.cache._clear("private")
        self.assertFalse(marker.encode() in self.cache.path.read_bytes())

    def test_missing_mapping_does_not_reschedule_disk_work(self):
        self.save("existing")
        self.cache.reclaimer.next_run = 12345
        self.cache.next_cleanup = 23456
        self.cache._clear("not-present")
        self.assertEqual(self.cache.reclaimer.next_run, 12345)
        self.assertEqual(self.cache.next_cleanup, 23456)


class SQLiteReclamationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "database.sqlite"
        self.lock, self.busy = threading.Lock(), threading.Event()

    def populate(self, wal=False, rows=40):
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE values_to_keep(id INTEGER PRIMARY KEY,data BLOB)")
        connection.executemany("INSERT INTO values_to_keep VALUES(?,?)", [(i, b"x" * 32768) for i in range(rows)])
        connection.commit()
        connection.execute("DELETE FROM values_to_keep WHERE id>0")
        connection.commit()
        return connection

    def reclaimer(self):
        return SQLiteReclaimer(self.path, self.lock, min_free_bytes=1024)

    def test_idle_reclamation_reduces_file_and_preserves_remaining_data(self):
        connection = self.populate()
        before = self.path.stat().st_size
        result = self.reclaimer().run(self.busy, force=True)
        self.assertEqual(result["state"], "reclaimed")
        self.assertLess(self.path.stat().st_size, before)
        self.assertEqual(connection.execute("SELECT data FROM values_to_keep").fetchone()[0], b"x" * 32768)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_wal_is_checkpointed_and_truncated(self):
        connection = self.populate(wal=True)
        wal = Path(str(self.path) + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        result = self.reclaimer().run(self.busy, force=True)
        self.assertEqual(result["state"], "reclaimed")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
        self.assertEqual(connection.execute("SELECT count(*) FROM values_to_keep").fetchone()[0], 1)

    def test_busy_request_or_writer_lock_defers_reclamation(self):
        self.populate()
        before = self.path.stat().st_size
        reclaimer = self.reclaimer()
        self.busy.set()
        self.assertEqual(reclaimer.run(self.busy, force=True)["state"], "busy")
        self.busy.clear()
        with self.lock:
            self.assertEqual(reclaimer.run(self.busy, force=True)["state"], "busy")
        self.assertEqual(self.path.stat().st_size, before)

    def test_external_sqlite_writer_causes_bounded_deferral(self):
        connection = self.populate()
        connection.execute("BEGIN IMMEDIATE")
        result = self.reclaimer().run(self.busy, force=True)
        connection.rollback()
        self.assertEqual(result["state"], "deferred")
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_new_request_interrupts_vacuum_and_leaves_database_valid(self):
        connection = self.populate(rows=200)
        connection.executemany("INSERT INTO values_to_keep VALUES(?,?)", [(i, b"keep") for i in range(1, 601)])
        connection.commit()
        class StartingRequest:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 2
        result = self.reclaimer().run(StartingRequest(), force=True)
        self.assertEqual(result["state"], "deferred")
        self.assertEqual(connection.execute("SELECT count(*) FROM values_to_keep").fetchone()[0], 601)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertFalse(Path(str(self.path) + "-journal").exists())

    def test_periodic_checks_are_rate_limited(self):
        self.populate()
        reclaimer = self.reclaimer()
        reclaimer.run(self.busy)
        with patch("packet_inspector.maintenance.sqlite3.connect", side_effect=AssertionError("unexpected repeat")):
            reclaimer.run(self.busy)


class GatedModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        compact = "compaction" in value.get("client_metadata", {}).get("x-codex-turn-metadata", "")
        body = complete()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not compact:
            self.server.waiting.set()
            self.server.release.wait(5)
        self.wfile.write(body)


class CleanupRelayTests(unittest.TestCase):
    def test_mapping_stays_protected_until_original_http_response_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), GatedModel)
            upstream.waiting, upstream.release = threading.Event(), threading.Event()
            server = InspectorServer(("127.0.0.1", 0), root / "data", config,
                                     "http://127.0.0.1:" + str(upstream.server_port))
            server.settings.update(packet_size_protection_enabled=True, request_compression="off", budget_bytes=4000)
            server.overflow.max_entries = 0
            results = []
            def post():
                client = http.client.HTTPConnection("127.0.0.1", server.port, timeout=8)
                path = server.endpoint.split(str(server.port), 1)[1] + "/responses"
                client.request("POST", path, make_body([message(i) for i in range(14)]), {"session_id": "inflight"})
                response = client.getresponse()
                results.append((response.status, response.read()))
                client.close()
            for service in (upstream, server):
                threading.Thread(target=service.serve_forever, daemon=True).start()
            request = threading.Thread(target=post)
            request.start()
            try:
                self.assertTrue(upstream.waiting.wait(4))
                server.overflow.maintenance(force=True)
                with closing(sqlite3.connect(server.overflow.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM mappings").fetchone()[0], 1)
                upstream.release.set()
                request.join(timeout=5)
                self.assertEqual(results, [(200, complete())])
                deadline = time.monotonic() + 2
                while server.active_requests and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(server.active_requests, 0)
                server.overflow.maintenance(force=True)
                with closing(sqlite3.connect(server.overflow.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM mappings").fetchone()[0], 0)
            finally:
                upstream.release.set()
                request.join(timeout=5)
                for service in (server, upstream):
                    service.shutdown()
                    service.server_close()

    def test_background_cleanup_runs_when_overflow_feature_is_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="test"', encoding="utf8")
            server = InspectorServer(("127.0.0.1", 0), root / "data", config, "http://127.0.0.1:1")
            try:
                server.settings["packet_size_protection_enabled"] = False
                server.overflow._save("expired", "scope", 1, [message(1)], [{"role": "user", "content": "old"}])
                with server.overflow._connect() as db:
                    db.execute("UPDATE mappings SET updated=?", (time.time() - 8 * 86400,))
                server.overflow.next_cleanup = 0.0
                deadline = time.monotonic() + 5
                remaining = 1
                while remaining and time.monotonic() < deadline:
                    with closing(sqlite3.connect(server.overflow.path)) as db:
                        remaining = db.execute("SELECT count(*) FROM mappings").fetchone()[0]
                    time.sleep(.02)
                self.assertFalse(server.settings["packet_size_protection_enabled"])
                self.assertEqual(remaining, 0)
                self.assertEqual(server.maintenance_errors, 0)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
