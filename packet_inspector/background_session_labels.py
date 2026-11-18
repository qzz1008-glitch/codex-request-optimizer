"""Bounded, asynchronous lookups for session indexes outside Windows."""
from collections import OrderedDict
import threading
import time

from .request_context import session_id
from .session_summary import MAX_SESSIONS


class BackgroundSessionLabels:
    def __init__(self, find_labels, *, batch_size=32):
        self.find_labels = find_labels
        self.batch_size = batch_size
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
                    threading.Thread(target=self._refresh, args=(pending[:self.batch_size], self.generation),
                                     daemon=True).start()
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
                if generation == self.generation:
                    for request in requests:
                        candidates = tuple(matches.get(request, ())[:2])
                        self.cache[request] = (now + (30 if candidates else 15), candidates)
                        self.cache.move_to_end(request)
                    while len(self.cache) > MAX_SESSIONS * 2:
                        self.cache.popitem(last=False)
                self.refreshing = False
