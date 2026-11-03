"""An explicitly confirmed, interrupting upgrade for pre-handover sharing workers.

Only runtime services are paused. Credentials and their ledgers are never edited.
The old worker must exit before borrowing is restored: older workers restore the
shared provider configuration on exit, even after they have disconnected.
"""
import http.client
import json

from .router import control_worker, local_json, process_alive, worker_info
from .startup_diagnostics import wait_for_startup

CANCELLED_EXIT = 2


class UpgradeCancelled(ValueError):
    pass


def sharing_state(status):
    host, client = status.get("host", {}), status.get("client", {})
    egress = status.get("egress", {})
    exit_host, selection = egress.get("host", {}), egress.get("selection", {})
    result = {}
    for key, active, values in (
        ("quota_out", host.get("running") or host.get("active"), host),
        ("network_out", exit_host.get("active"), exit_host),
    ):
        if active:
            address, port = values.get("address"), values.get("port")
            if not isinstance(address, str) or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("无法确认共享监听地址，已保留当前服务。")
            result[key] = {"address": address, "port": port}
    for key, active, values in (
        ("quota_in", client.get("active"), client),
        ("network_in", selection.get("mode") == "peer", selection),
    ):
        if active:
            identifier = values.get("active_id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("无法确认借入凭据，已保留当前服务。")
            result[key] = {"id": identifier}
    return result


class SharingUpgrade:
    def __init__(self, data_dir, source, status, record):
        self.data_dir, self.source = data_dir, source
        self.snapshot = sharing_state(status)
        self.record = record
        self.paused = False
        self.source_pid = None

    def _verification_failed(self, identity, reason, message, **details):
        self.record("source_verification_failed", {"reason": reason, "worker_id": identity, **details})
        raise ValueError(message)

    def _verify_source_process(self, identity, worker, health):
        pid = health.get("pid")
        if pid is None:
            # Pre-handover workers identify themselves in /health but publish
            # their PID only in this per-worker runtime record. The filename
            # binds it to the verified identity; the port must also match.
            runtime_path = self.data_dir / "router-workers" / (identity + ".runtime.json")
            message = "无法确认旧版服务进程记录，已保留当前服务。"
            try:
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._verification_failed(identity, "runtime_unreadable", message, error_type=type(exc).__name__)
            if not isinstance(runtime, dict) or type(runtime.get("port")) is not int or runtime["port"] != worker["port"]:
                self._verification_failed(identity, "runtime_port_mismatch", message, port=worker["port"])
            pid = runtime.get("pid")
        if type(pid) is not int or pid <= 0:
            self._verification_failed(identity, "invalid_pid", "无法确认待停止的旧版共享进程。")
        if self.source_pid is not None and pid != self.source_pid:
            self._verification_failed(identity, "pid_changed", "共享服务已变化，请重新执行升级。",
                                      previous_pid=self.source_pid, pid=pid)
        self.source_pid = pid

    def api(self, identity, data=None):
        worker = worker_info(self.data_dir, identity)
        health = local_json(worker["port"], "/health", timeout=3)
        if health.get("worker_id") != identity:
            self._verification_failed(identity, "worker_id_mismatch", "共享服务已变化，请重新执行升级。")
        if identity == self.source:
            self._verify_source_process(identity, worker, health)
        return local_json(worker["port"], "/api/sharing", data, timeout=30)

    def pause(self, identity, *, check=False):
        current = sharing_state(self.api(identity))
        if check and current != self.snapshot:
            raise ValueError("共享状态已变化，请重新执行升级并确认。")
        if check:
            # A timed-out action may already have taken effect. Rollback must
            # attempt recovery even if its response was never received.
            self.paused = True
        self.record("pausing")
        for key, action in (("quota_in", "client_disconnect"), ("quota_out", "host_stop"),
                            ("network_in", "egress_select"), ("network_out", "egress_stop")):
            if key in current:
                self.api(identity, {"action": action, **({"mode": "local"} if key == "network_in" else {})})

    def stop_source(self):
        if self.source_pid is None:
            raise ValueError("无法确认待停止的旧版共享进程。")
        pid = self.source_pid
        self.record("waiting_for_requests")

        def stop():
            if not process_alive(pid):
                return True  # The entry may already have retired this drained worker.
            shared = self.api(self.source)
            if any(host.get("in_flight", 0) for host in (shared.get("host", {}), shared.get("egress", {}).get("host", {}))):
                return None
            try:
                control_worker(self.data_dir, self.source, "stop")
            except ValueError as exc:
                if str(exc) == "control HTTP 409":
                    return None  # Old versions reject stop while a local request is running.
                raise
            return True

        wait_for_startup(stop, 120, errors=(OSError, http.client.HTTPException), interval=.25,
                         timeout=lambda: TimeoutError("旧请求尚未结束，已取消切换并恢复原服务，请稍后升级。"))
        wait_for_startup(lambda: True if not process_alive(pid) else None, 10, errors=(), interval=.1,
                         timeout=lambda: TimeoutError("旧版共享进程尚未退出，正在恢复服务。"))
        self.record("source_stopped")

    def restore(self, identity):
        if identity == self.source:
            self.source_pid = None  # Rollback may have restarted this registered worker.
        self.record("restoring")
        current = sharing_state(self.api(identity))
        for key in ("network_out", "network_in", "quota_out", "quota_in"):
            if key not in self.snapshot or current.get(key) == self.snapshot[key]:
                continue
            values = self.snapshot[key]
            if key.endswith("_out"):
                action = "host_resume" if key == "quota_out" else "egress_resume"
                self.api(identity, {"action": action, **values})
            else:
                self.api(identity, {"action": "credential_use", "kind": "quota" if key == "quota_in" else "exit", **values})
        if sharing_state(self.api(identity)) != self.snapshot:
            raise ValueError("共享状态未能完整恢复，请查看升级结果。")
        self.record("restored")

