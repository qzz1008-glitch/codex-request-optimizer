"""Reconcile an existing connection after startup or committed worker takeover."""
from copy import deepcopy
import json
import secrets
import threading
import time

from .network_diagnostics import safe_text


class ConnectionResume:
    def __init__(self, server):
        self.server = server
        self.guard = threading.RLock()
        self.ready = False
        self.pending = not bool(server.worker_id)
        self.deferred_wsl = set()
        self.next_wsl_scan = 0
        self.current = {"id": "", "state": "pending" if self.pending else "idle", "result": {}, "error": ""}

    def status(self):
        with self.guard:
            return deepcopy(self.current)

    def schedule(self):
        with self.guard:
            self.pending = True
            self.current = {"id": secrets.token_hex(12), "state": "pending", "result": {}, "error": ""}
            return deepcopy(self.current)

    def cancel(self):
        with self.guard:
            self.pending = False
            self.deferred_wsl.clear()
            self.current = {"id": "", "state": "idle", "result": {}, "error": ""}

    def _active(self):
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
        if not self._active():
            raise ValueError("当前后台已切换，已停止同步。")
        with self.server.guard:
            self.server.save_settings({**self.server.settings, "provider_routes": routes})

    @staticmethod
    def _wsl_result(result):
        return {"changed": bool(result.get("changed")), "reload_required": bool(result.get("reload_required")),
                **{key: result.get(key, [])[:16] for key in ("targets", "detected", "warnings", "deferred")}}

    def _watch_deferred(self):
        if not self.deferred_wsl or time.monotonic() < self.next_wsl_scan or not self._active():
            return
        self.next_wsl_scan = time.monotonic() + 30
        server = self.server
        with server.provider_guard:
            try:
                if not server.settings["auto_detect_providers"] or not server.connection_profile()["active"]:
                    self.deferred_wsl.clear()
                    return
                running = set(server.wsl.backend.running())
            except (OSError, ValueError):
                return
            if running & self.deferred_wsl:
                self.schedule()

    def step(self):
        # Called by the background thread, never the HTTP/control handler.
        if not self.pending:
            self._watch_deferred()
        if not self.pending or not self._active():
            return
        server = self.server
        with server.provider_guard:
            if not self.pending or not self._active():
                return
            with self.guard:
                self.pending = False
                self.current = {"id": self.current.get("id") or secrets.token_hex(12),
                                "state": "checking", "result": {}, "error": ""}
            started = time.monotonic()
            result, error = {"active": False, "reload_required": False}, ""
            try:
                if server.settings["auto_detect_providers"] and server.connection_profile()["active"]:
                    provider = server.sync_providers()
                    wsl = {}
                    if not server.sharing.remote:
                        wsl = server.wsl.apply(True, server.endpoint, server.settings["upstream"],
                            server.settings["provider_routes"],
                            self._publish_routes,
                            retry_limit=server.settings["capacity_retry_max_retries"]
                            if server.settings["capacity_retry_enabled"] else None)
                    reload_required = bool(provider.get("reload_required") or wsl.get("reload_required")
                                           or server.connection_reload_required)
                    result = {"active": True, "changed": bool(provider.get("changed") or wsl.get("changed")),
                              "reload_required": reload_required, "warnings": provider.get("warnings", [])[:16],
                              "wsl": self._wsl_result(wsl)}
                    server.connection_reload_required |= reload_required
                    self.deferred_wsl = {item["distribution"] for item in wsl.get("deferred", [])}
                    self.next_wsl_scan = time.monotonic() + 30
            except Exception as exc:
                # Keep serving already-published routes and report the repair.
                error = safe_text(type(exc).__name__ + ": " + str(exc), (server.settings["relay_token"],), limit=1000)
                self.deferred_wsl.clear()
            attention = bool(error or result.get("warnings") or result.get("wsl", {}).get("warnings"))
            with self.guard:
                self.current = {**self.current, "state": "attention" if attention else "complete",
                                "result": result, "error": error,
                                "duration_ms": round((time.monotonic() - started) * 1000)}
                record = {**self.current, "worker_id": server.worker_id}
            if not self._active():
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
