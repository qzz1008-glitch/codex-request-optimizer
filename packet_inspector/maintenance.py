"""Bounded, best-effort reclamation of SQLite free space during idle periods."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time


class SQLiteReclaimer:
    def __init__(self, path, lock, *, interval=600, min_free_bytes=4 * 1024 * 1024,
                 min_free_ratio=.25, time_budget=2):
        self.path, self.lock = Path(path), lock
        self.interval, self.min_free_bytes = interval, min_free_bytes
        self.min_free_ratio, self.time_budget = min_free_ratio, time_budget
        self.next_run = 0.0
        self.last = {"state": "not_run", "reclaimed_bytes": 0}

    def request(self):
        self.next_run = 0.0

    def _disk_bytes(self):
        total = 0
        for path in (self.path, Path(str(self.path) + "-wal")):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def run(self, busy: threading.Event, *, force=False):
        now = time.monotonic()
        if not force and now < self.next_run:
            return self.last
        if not self.path.exists():
            self.next_run = now + self.interval
            self.last = {"state": "absent", "reclaimed_bytes": 0}
            return self.last
        if busy.is_set() or not self.lock.acquire(blocking=False):
            self.last = {"state": "busy", "reclaimed_bytes": 0}
            return self.last
        connection = None
        deadline = now + self.time_budget
        try:
            before = self._disk_bytes()
            # Re-check after acquiring the writer lock. New requests set busy,
            # and the progress callback cancels an already-running VACUUM.
            if busy.is_set():
                self.last = {"state": "busy", "reclaimed_bytes": 0}
                return self.last
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=.05)
            connection.set_progress_handler(lambda: int(busy.is_set() or time.monotonic() >= deadline), 1000)
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            free = connection.execute("PRAGMA freelist_count").fetchone()[0]
            size = connection.execute("PRAGMA page_size").fetchone()[0]
            vacuum = free * size >= self.min_free_bytes and free >= pages * self.min_free_ratio
            if vacuum:
                connection.execute("VACUUM")
            # WAL can retain space independently of the database freelist.
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            deferred = bool(checkpoint and checkpoint[0])
            self.next_run = now + (5 if deferred else self.interval)
            self.last = {"state": "deferred" if deferred else "reclaimed" if vacuum else "checked",
                         "reclaimed_bytes": max(0, before - self._disk_bytes())}
        except sqlite3.OperationalError as exc:
            expected = getattr(exc, "sqlite_errorcode", None) in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}
            self.next_run = now + 5
            self.last = {"state": "deferred" if expected else "error", "reclaimed_bytes": 0}
        finally:
            if connection is not None:
                connection.close()
            self.lock.release()
        return self.last
