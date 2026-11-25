"""Reconcile an existing connection after startup or committed worker takeover."""
from copy import deepcopy
import json
import secrets
import threading
import time

from .network_diagnostics import safe_text
from .loopback_proxy import ProxyRepairError
from .providers import ProviderManager
from . import VERSION


RECOVERY_DELAYS = (3, 10)


class ConnectionResume:
    def __init__(self, server):
        self.server = server
        self.guard = threading.RLock()
        self.ready = False
        self.pending = not bool(server.worker_id)
        self.deferred_wsl = set()
        self.waiting_wsl = set()
        self.waiting_local = False
        self.next_wsl_scan = 0
        self.attempt = 0
        self.retry_at = 0
        self.current = {"id": "", "state": "pending" if self.pending else "idle", "result": {}, "error": ""}

    def status(self):
        with self.guard:
            return deepcopy(self.current)

    def schedule(self):
        with self.guard:
            self.attempt = 0
            self.retry_at = 0
            self.pending = True
            self.current = {"id": secrets.token_hex(12), "state": "pending", "result": {}, "error": ""}
            return deepcopy(self.current)

    def cancel(self):
        with self.guard:
            self.pending = False
            self.retry_at = 0
            self.attempt = 0
            self.deferred_wsl.clear()
            self.waiting_wsl.clear()
            self.waiting_local = False
            self.current = {"id": "", "state": "idle", "result": {}, "error": ""}

    def is_active(self):
        server = self.server
        if not self.ready or server.worker_state != "active" or server.upgrade_draining or server.analysis_stop.is_set():
            return False
        if not server.worker_id:
            return True
        try:
            state = json.loads((server.data_dir / "router-state.json").read_text(encoding="utf8"))
            return isinstance(state, dict) and state.get("active") == server.worker_id
        except (OSError, ValueError):
            return False

    def retry(self):
        with self.server.provider_guard:
            if not self.server.settings["auto_detect_providers"] or not self.server.connection_profile()["active"]:
                raise ValueError("当前未接入，无需同步。")
            return self.schedule()

    def _publish_routes(self, routes):
        if not self.is_active():
            raise ValueError("当前后台已切换，已停止同步。")
        with self.server.guard:
            self.server.save_settings({**self.server.settings, "provider_routes": routes})

    def diagnostics(self):
        server = self.server
        secrets = (server.settings.get("relay_token", ""), server.worker_token)

        def clean(value, depth=0):
            if depth > 12:
                return "[limit]"
            if isinstance(value, str):
                return safe_text(value, secrets, limit=24000)
            if isinstance(value, dict):
                return {str(key): clean(item, depth + 1) for key, item in list(value.items())[:64]}
            if isinstance(value, list):
                return [clean(item, depth + 1) for item in value[:500]]
            return value

        return clean({"version": 1, "application_version": VERSION, "created": time.time(),
                      "log_directory": str(server.log_dir),
                      "connection_snapshot": str(server.data_dir / "connection-resume.json"),
                      "connection": self.status(), "provider_error": server.provider_sync_error,
                      "provider_warnings": server.provider_sync_result.get("warnings", []),
                      "wsl": self._wsl_result(server.wsl.result),
                      "network": server.network_diagnostics.export()})

    @staticmethod
    def _wsl_result(result):
        return {"changed": bool(result.get("changed")), "reload_required": bool(result.get("reload_required")),
                **{key: result.get(key, [])[:16] for key in ("targets", "detected", "warnings", "deferred")}}

    def track_migrations(self, local, wsl):
        self.deferred_wsl = {item["distribution"] for item in wsl.get("deferred", [])}
        self.waiting_local = bool(local.get("waiting_for_reload"))
        self.waiting_wsl = {target["distribution"] for target in wsl.get("targets", [])
                            if target.get("migration_waiting")}
        self.next_wsl_scan = time.monotonic() + 30

    def _watch_deferred(self):
        if (not (self.deferred_wsl or self.waiting_wsl or self.waiting_local)
                or time.monotonic() < self.next_wsl_scan or not self.is_active()):
            return
        self.next_wsl_scan = time.monotonic() + 30
        server = self.server
        with server.provider_guard:
            try:
                if self.waiting_local:
                    self.schedule()
                    return
                running = set(server.wsl.backend.running())
            except (OSError, ValueError):
                return
            if running & (self.deferred_wsl | self.waiting_wsl):
                self.schedule()

    def step(self):
        # Called by the background thread, never the HTTP/control handler.
        with self.guard:
            if self.retry_at:
                if time.monotonic() < self.retry_at:
                    return
                self.retry_at = 0
                self.pending = True
        if not self.pending:
            self._watch_deferred()
        if not self.pending or not self.is_active():
            return
        server = self.server
        with server.provider_guard:
            if not self.pending or not self.is_active():
                return
            with self.guard:
                self.pending = False
                self.attempt += 1
                self.current = {"id": self.current.get("id") or secrets.token_hex(12),
                                "state": "checking", "result": {}, "error": "",
                                "attempt": self.attempt, "max_attempts": len(RECOVERY_DELAYS) + 1}
            started = time.monotonic()
            result, error = {"active": False, "reload_required": False, "local_reload_required": False}, ""
            wsl = {}
            previously_waiting_wsl = self.waiting_wsl
            try:
                if server.settings["auto_detect_providers"] and server.connection_profile()["active"]:
                    provider = server.sync_providers()
                    if not server.sharing.remote:
                        wsl = server.wsl.apply(True, server.endpoint, server.settings["upstream"],
                            server.settings["provider_routes"],
                            self._publish_routes,
                            retry_limit=server.settings["capacity_retry_max_retries"]
                            if server.settings["capacity_retry_enabled"] else None)
                    reload_required = bool(provider.get("reload_required") or wsl.get("reload_required")
                                           or server.connection_reload_required)
                    result = {"active": True, "changed": bool(provider.get("changed") or wsl.get("changed")),
                              "local_reload_required": bool(provider.get("reload_required")),
                              "reload_required": reload_required, "warnings": provider.get("warnings", [])[:16],
                              "session_migration": provider.get("session_migration", {}),
                              "wsl": self._wsl_result(wsl)}
                    server.connection_reload_required |= reload_required
                    if not server.sharing.remote:
                        try:
                            loopback = server.repair_local_loopback(codex_only=True)
                        except ProxyRepairError as exc:
                            result["loopback_proxy"] = exc.report
                            raise
                        if loopback.get("changes"):
                            result["loopback_proxy"] = loopback
                        if loopback["changed"]:
                            result.update(changed=True, local_reload_required=True, reload_required=True)
                            server.connection_reload_required = True
                            server.restart_notices.mark("connection")
                elif not server.settings["auto_detect_providers"] and not server.sharing.remote:
                    migration = ProviderManager(server.link, server.endpoint, server.settings["upstream"]).reconcile_sessions(
                        server.settings["provider_routes"])
                    local_changed = bool(migration["changed"])
                    local_reload = bool(migration.get("waiting_for_reload")
                                        or local_changed and not migration.get("verified_after_reload"))
                    server.provider_sync_result["warnings"] = migration["warnings"]
                    result.update(changed=local_changed, local_reload_required=local_reload,
                                  reload_required=local_reload, warnings=migration["warnings"][:16],
                                  session_migration=migration)
                    server.connection_reload_required |= local_reload
                    if local_reload and (local_changed or not self.waiting_local):
                        server.restart_notices.mark("connection")
                    wsl = server.wsl.reconcile_sessions(server.endpoint, server.settings["upstream"],
                                                       server.settings["provider_routes"])
                    result["changed"] |= bool(wsl.get("changed"))
                    result["reload_required"] |= bool(wsl.get("reload_required"))
                    result["wsl"] = self._wsl_result(wsl)
                    server.connection_reload_required |= result["reload_required"]
                self.track_migrations(result.get("session_migration", {}), wsl)
            except Exception as exc:
                # Keep serving already-published routes and report the repair.
                error = safe_text(type(exc).__name__ + ": " + str(exc), (server.settings["relay_token"],), limit=1000)
                self.deferred_wsl.clear()
            for target in result.get("wsl", {}).get("targets", []):
                distribution = str(target.get("distribution", ""))
                if (target.get("reload_required", target.get("changed"))
                        and (target.get("changed") or distribution not in previously_waiting_wsl)):
                    server.restart_notices.mark("wsl", target="wsl", scope=distribution[:160])
            result["restart_notice"] = server.restart_notices.snapshot()
            attention = bool(error or result.get("warnings") or result.get("wsl", {}).get("warnings"))
            waiting = self.waiting_local or bool(self.waiting_wsl)
            recoverable = bool(error or any(row.get("reason") not in {"config_not_found", "restore_pending"}
                                           for row in result.get("wsl", {}).get("warnings", [])))
            with self.guard:
                delay = (RECOVERY_DELAYS[self.attempt - 1]
                         if attention and recoverable and self.attempt <= len(RECOVERY_DELAYS) else 0)
                self.retry_at = time.monotonic() + delay if delay else 0
                state = "recovering" if delay else "attention" if attention else "awaiting_reload" if waiting else "complete"
                self.current = {**self.current, "state": state,
                                "result": result, "error": error,
                                "retry_in_seconds": delay, "updated": time.time(),
                                "duration_ms": round((time.monotonic() - started) * 1000)}
                record = {**self.current, "worker_id": server.worker_id}
            if not self.is_active():
                return
            try:
                # One bounded snapshot; startup checks do not append a log or
                # invoke the external proxy/environment probe used by Connect.
                path = server.data_dir / "connection-resume.json"
                staging = path.with_suffix(".new")
                staging.write_text(json.dumps(record, ensure_ascii=False), encoding="utf8")
                staging.replace(path)
            except OSError:
                pass  # The live status still exposes the complete result.
