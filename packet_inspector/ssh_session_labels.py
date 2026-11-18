"""Resolve captured SSH sessions without waiting for SSH on the HTTP threads."""
from collections import OrderedDict, defaultdict
import threading
import time

from .remote_connections import saved_hosts
from .remote_control import SSH_LABEL_BATCH
from .request_context import session_id
from .session_summary import MAX_SESSIONS, external_labels


class SshSessionLabels:
    def __init__(self, relays, codex_home, ssh_config):
        self.relays = relays
        self.codex_home, self.ssh_config = codex_home, ssh_config
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.refreshing = False

    def __call__(self, origins):
        if not origins or self.relays.closed:
            return {}
        requests = [(thread, key) for thread, key in origins.items() if thread and session_id(thread) == thread]
        now = time.monotonic()
        with self.lock:
            if not self.refreshing:
                pending = [request for request in requests
                           if request not in self.cache or self.cache[request][0] <= now]
                pending.sort(key=lambda request: self.cache.get(request, (0, None))[0])
                if pending:
                    self.refreshing = True
                    threading.Thread(target=self._refresh, args=(pending[:SSH_LABEL_BATCH],), daemon=True).start()
            return {request[0]: self.cache[request][1] for request in requests
                    if request in self.cache and self.cache[request][1]}

    def _refresh(self, requests):
        matches = defaultdict(list)
        try:
            routes = defaultdict(dict)
            for key, record in self.relays.records():
                if record.get("enabled"):
                    routes[key.partition("-")[0]][key] = record
            if routes:
                for host in saved_hosts(self.codex_home, self.ssh_config):
                    if self.relays.closed:
                        break
                    keys = routes.get(self.relays.key(host), {})
                    if not keys:
                        continue
                    grouped = defaultdict(set)
                    for thread, key in requests:
                        if key is None or key in keys:
                            grouped[key].add(thread)
                    for key, ids in grouped.items():
                        record = keys.get(key, {})
                        if key and not record.get("home"):
                            continue
                        account = {field: record[field] for field in ("home", "uid") if field in record}
                        self._query(host, ids, key, account, keys, matches)
        except (OSError, ValueError):
            pass
        finally:
            now = time.monotonic()
            with self.lock:
                for request in requests:
                    candidates = matches[request]
                    # Older captures have no route key. If more than one host
                    # claims a thread ID, keep its name unresolved.
                    label = candidates[0] if len(candidates) == 1 else None
                    self.cache[request] = (now + (30 if label else 15), label)
                    self.cache.move_to_end(request)
                while len(self.cache) > MAX_SESSIONS * 2:
                    self.cache.popitem(last=False)
                self.refreshing = False

    def _query(self, host, ids, key, account, routes, matches):
        try:
            result = self.relays.ssh(host, "tunnel_labels", ids=sorted(ids), **account)
            if result.get("error"):
                return
            labels = external_labels(result, ids)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return
        # For older captures, use the authenticated account to constrain any
        # follow-up parent lookup if its route is still known.
        home, uid = result.get("home"), result.get("uid")
        origin = key or next((scope for scope, record in routes.items()
                              if home and record.get("home") == home and record.get("uid", uid) == uid), None)
        for thread, label in labels.items():
            matches[thread, key].append({**label, **({"ssh_connection_key": origin} if origin else {})})
