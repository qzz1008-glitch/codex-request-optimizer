"""Bounded, asynchronous lookups for session indexes outside Windows."""
from collections import OrderedDict
import threading
import time

from .request_context import session_id
from .session_summary import MAX_SESSIONS


class SessionLabelUpdates:
    """Wake sidebar requests after shared SSH and WSL cache changes."""

    def __init__(self):
        self._condition = threading.Condition()
        self._revision = 0
        self._active = 0

    def revision(self):
        with self._condition:
            return self._revision

    def needs_wait(self, after):
        with self._condition:
            return self._active > 0 or self._revision != after

    def begin(self):
        with self._condition:
            self._active += 1

    def complete(self, changed):
        with self._condition:
            self._active -= 1
            if changed:
                self._revision += 1
            self._condition.notify_all()

    def wait(self, after, timeout=15):
        with self._condition:
            self._condition.wait_for(lambda: self._revision != after or self._active == 0, timeout)
            changed = self._revision != after
            return {"changed": changed, "pending": not changed and self._active > 0}


class BackgroundSessionLabels:
    def __init__(self, find_labels, *, batch_size=32, updates=None):
        self.find_labels = find_labels
        self.batch_size = batch_size
        self.updates = updates if updates is not None else SessionLabelUpdates()
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.refreshing = False
        self.generation = 0

    def __call__(self, origins):
        """Return up to two candidates per completed lookup, including misses."""
        if not origins:
            return {}
        requests = [(thread, key, host_id) for thread, (key, host_id) in origins.items()
                    if thread and session_id(thread) == thread]
        now = time.monotonic()
        with self.lock:
            if not self.refreshing:
                pending = [request for request in requests
                           if request not in self.cache or self.cache[request][0] <= now]
                pending.sort(key=lambda request: self.cache.get(request, (0, None))[0])
                if pending:
                    self.refreshing = True
                    self.updates.begin()
                    try:
                        threading.Thread(target=self._refresh, args=(pending[:self.batch_size], self.generation),
                                         daemon=True).start()
                    except RuntimeError:
                        self.refreshing = False
                        self.updates.complete(False)
            return {request[0]: self.cache[request][1] for request in requests if request in self.cache}

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.generation += 1

    def _refresh(self, requests, generation):
        matches = {}
        try:
            matches = self.find_labels(requests)
        except (OSError, ValueError):
            pass
        finally:
            now = time.monotonic()
            with self.lock:
                invalidated = generation != self.generation
                changed = invalidated
                try:
                    if not invalidated:
                        for request in requests:
                            candidates = tuple(matches.get(request, ())[:2])
                            changed = changed or self.cache.get(request, (0, None))[1] != candidates
                            self.cache[request] = (now + (30 if candidates else 15), candidates)
                            self.cache.move_to_end(request)
                        while len(self.cache) > MAX_SESSIONS * 2:
                            self.cache.popitem(last=False)
                finally:
                    self.refreshing = False
                    self.updates.complete(changed)
