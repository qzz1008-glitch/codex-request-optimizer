"""Resolve captured SSH sessions without waiting for SSH on the HTTP threads."""
from collections import defaultdict

from .background_session_labels import BackgroundSessionLabels
from .native_state import remote_address
from .remote_connections import saved_hosts
from .remote_control import SSH_LABEL_BATCH
from .session_summary import external_labels


class SshSessionLabels(BackgroundSessionLabels):
    def __init__(self, relays, codex_home, ssh_config, *, updates=None):
        super().__init__(self._find_labels, batch_size=SSH_LABEL_BATCH, updates=updates)
        self.relays = relays
        self.codex_home, self.ssh_config = codex_home, ssh_config

    def __call__(self, origins):
        return {} if self.relays.closed else super().__call__(origins)

    def route_origins(self, keys):
        """Resolve previously captured route keys using local SSH settings."""
        try:
            hosts = saved_hosts(self.codex_home, self.ssh_config)
        except (OSError, ValueError, RuntimeError):
            return {}
        by_host = {}
        for host in hosts:
            address = host.address or remote_address(host.destination)
            if address:
                by_host[self.relays.key(host)] = {"kind": "remote", "address": address}
        origins = {}
        for key in keys:
            # Older routes used the host key alone. Newer routes append an account hash.
            origin = by_host.get(key.partition("-")[0])
            if origin:
                origins[key] = origin
        return origins

    def _find_labels(self, requests):
        matches = defaultdict(list)
        routes = defaultdict(dict)
        for key, record in self.relays.records():
            if record.get("enabled"):
                routes[key.partition("-")[0]][key] = record
        if not routes:
            return matches
        for host in saved_hosts(self.codex_home, self.ssh_config):
            if self.relays.closed:
                break
            keys = routes.get(self.relays.key(host), {})
            if not keys:
                continue
            grouped = defaultdict(dict)
            for request in requests:
                thread, key, host_id = request
                if key in keys or (key is None and host_id in (None, host.id)):
                    grouped[key][thread] = request
            for key, batch in grouped.items():
                record = keys.get(key, {})
                if key and not record.get("home"):
                    continue
                account = {field: record[field] for field in ("home", "uid") if field in record}
                self._query(host, batch, key, account, keys, matches)
        return matches

    def _query(self, host, batch, key, account, routes, matches):
        try:
            result = self.relays.ssh(host, "tunnel_labels", ids=sorted(batch), **account)
            if result.get("error"):
                return
            labels = external_labels(result, batch)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return
        # For older captures, use the authenticated account to constrain any
        # follow-up parent lookup if its route is still known.
        home, uid = result.get("home"), result.get("uid")
        origin = key or next((scope for scope, record in routes.items()
                              if home and record.get("home") == home and record.get("uid", uid) == uid), None)
        for thread, label in labels.items():
            matches[batch[thread]].append({**label, **({"ssh_connection_key": origin} if origin else {})})
