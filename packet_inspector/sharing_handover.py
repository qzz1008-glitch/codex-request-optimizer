"""Authenticated worker-to-worker transfer of live Windows sharing listeners."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time

from .router import atomic_json, control_worker, local_json, worker_info
from .locking import file_lock
from .sharing import LANServer, parse_link, pinned_opener, tls_context
from .shared_egress import ExitHandler

PROTOCOL = 1
UNSUPPORTED = "当前运行版本不支持共享无缝交接，已保留当前服务，本次升级未执行。"


def runtime(server):
    return {"quota_out": bool(server.sharing.listener), "quota_in": bool(server.sharing.remote),
            "network_out": bool(server.egress.listener),
            "network_in": server.egress.mode == "peer" and bool(server.egress.peer_link)}


def active_status(status):
    if isinstance(status.get("sharing_runtime"), dict):
        return any(status["sharing_runtime"].values())
    host, client, egress = status.get("host", {}), status.get("client", {}), status.get("egress", {})
    return bool(host.get("running") or host.get("active") or client.get("active")
                or egress.get("host", {}).get("active") or egress.get("selection", {}).get("mode") == "peer")


class SharingHandover:
    def __init__(self, server):
        self.server = server
        self.guard = threading.RLock()
        self.mode = self.phase = self.peer = self.ticket = ""
        self.deadline = 0
        self.listeners = []
        self.step = ""

    @property
    def pending(self):
        with self.guard:
            if self.phase not in {"prepared", "active", "yielded"}:
                return False
            if time.monotonic() >= self.deadline:
                # Recover a launcher that disappeared using the entry's durable
                # committed owner, never by guessing from a timeout alone.
                try:
                    state = json.loads((self.server.data_dir / "router-state.json").read_text(encoding="utf8"))
                    active = state["active"]
                    if active in {self.server.worker_id, self.peer}:
                        committed = active == (self.server.worker_id if self.mode == "import" else self.peer)
                        self.finish(committed)
                except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                    pass
            return self.phase in {"prepared", "active", "yielded"}

    def poll(self):
        # Runs on the existing background thread, including standby/draining
        # workers. No file reads or writes occur outside an expired transaction.
        with self.server.provider_guard:
            return self.pending

    def _record(self, phase, error=None):
        try:
            path = self.server.data_dir / "sharing-handover.json"
            with file_lock(path.with_suffix(".lock")):
                try:
                    report = json.loads(path.read_text(encoding="utf8"))
                except (OSError, ValueError):
                    report = {}
                if not isinstance(report, dict):
                    report = {}
                identifier = hashlib.sha256(self.ticket.encode()).hexdigest()[:12]
                if report.get("handover_id") != identifier:
                    report = {"handover_id": identifier, "events": []}
                event = {"updated": time.time(), "worker_id": self.server.worker_id, "peer": self.peer,
                         "direction": self.mode, "phase": phase, "step": self.step,
                         "runtime": runtime(self.server), "connections": self.connections()}
                if error is not None:
                    event["error_type"] = type(error).__name__
                    report["last_error"] = event
                events = report.get("events", [])
                report["events"] = [*(events if isinstance(events, list) else [])[-11:], event]
                atomic_json(path, report)
        except (OSError, ValueError, TypeError):
            pass

    def connections(self):
        listeners = {listener for listener in [self.server.sharing.listener, self.server.egress.listener, *self.listeners]
                     if listener is not None}
        return sum(listener.connections for listener in listeners)

    def export(self, target):
        with self.server.provider_guard, self.guard:
            if os.name != "nt" or self.server.worker_state != "active" or self.pending:
                raise ValueError("共享交接状态无效。")
            worker = worker_info(self.server.data_dir, target)
            health = local_json(worker["port"], "/health", timeout=3)
            if health.get("worker_id") != target or type(health.get("pid")) is not int:
                raise ValueError("无法确认新版共享进程。")
            quota, egress = self.server.sharing, self.server.egress
            self.mode, self.phase, self.peer, self.ticket = "export", "prepared", target, secrets.token_hex(16)
            self.deadline = time.monotonic() + 120
            self.listeners = [listener for listener in (quota.listener, egress.listener) if listener is not None]
            try:
                self.step = "callback_exit"
                # A borrowed quota request can create this listener lazily.
                # Prepare it before the snapshot so old and new requests use
                # the same callback even if the first request arrives now.
                if quota.remote and egress.mode == "local":
                    egress.request_exit(quota.remote)
                    if egress.listener not in self.listeners:
                        self.listeners.append(egress.listener)
                self.step = "duplicate_listeners"
                def listener_state(controller):
                    if controller.listener is None:
                        return None
                    host = dict(controller.host)
                    if isinstance(host.get("identity_hash"), bytes):
                        host["identity_hash"] = host["identity_hash"].hex()
                    return {"socket": base64.b64encode(controller.listener.socket.share(health["pid"])).decode("ascii"),
                            "host": host}
                state = {"protocol": PROTOCOL, "source": self.server.worker_id, "target": target, "ticket": self.ticket,
                         "quota": listener_state(quota), "exit": listener_state(egress),
                         "quota_in": quota.remote["credential_id"] if quota.remote else None,
                         "exit_in": egress.peer_id if egress.mode == "peer" else None,
                         "local_link": egress.local_link, "callback_link": egress.callback_link,
                         "role": quota.role.name if quota.role.resource is not None else None}
                self._record("prepared")
                return state
            except Exception as exc:
                self.phase = "cancelled"
                self._record("export_failed", exc)
                raise

    def stage(self, state):
        with self.server.provider_guard, self.guard:
            source, ticket = state.get("source"), state.get("ticket")
            active = json.loads((self.server.data_dir / "router-state.json").read_text(encoding="utf8"))["active"]
            if (os.name != "nt" or state.get("protocol") != PROTOCOL or state.get("target") != self.server.worker_id
                    or source != active or not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f]{32}", ticket)
                    or self.server.worker_state != "standby" or self.pending or any(runtime(self.server).values())):
                raise ValueError("共享交接来源无效。")
            worker_info(self.server.data_dir, source)
            quota, egress = self.server.sharing, self.server.egress
            self.mode, self.phase, self.peer, self.ticket = "import", "prepared", source, ticket
            self.deadline = time.monotonic() + 120
            self.listeners = []
            try:
                self.step = "sharing_role"
                if bool(state.get("role")) != bool(state.get("quota") or state.get("quota_in")):
                    raise ValueError("共享角色交接状态不完整。")
                if state.get("role"):
                    if state["role"] != quota.role.name:
                        raise ValueError("共享角色不匹配。")
                    quota.role.acquire(handover=True)
                if state.get("quota"):
                    self.step = "native_authorization"
                    quota.authorization = quota.authorization_factory()
                    identity = hashlib.sha256(quota.authorization.credentials()["identity"].encode()).hexdigest()
                    self.step = "account_identity"
                    if not secrets.compare_digest(identity, state["quota"]["host"]["identity_hash"]):
                        raise ValueError("Codex 登录在升级期间发生变化，未切换共享。")
                def adopt(value, controller, purpose, handler=None):
                    if value is None:
                        return
                    connection = socket.fromshare(base64.b64decode(value["socket"], validate=True))
                    try:
                        host = dict(value["host"])
                        if connection.getsockname()[:2] != (host["address"], host["port"]):
                            raise ValueError("共享监听地址不匹配。")
                        context, fingerprint = tls_context(host["address"], quota.vault, purpose)
                        if host.get("fingerprint") != fingerprint:
                            raise ValueError("共享证书在升级期间发生变化。")
                        if "identity_hash" in host:
                            host["identity_hash"] = bytes.fromhex(host["identity_hash"])
                        host["in_flight"] = 0
                        controller.host = host
                        controller.listener = LANServer.adopt(connection, controller, context, handler)
                        self.listeners.append(controller.listener)
                    except Exception:
                        connection.close()
                        raise
                self.step = "adopt_listeners"
                adopt(state.get("quota"), quota, "quota")
                adopt(state.get("exit"), egress, "exit", ExitHandler)
                if state.get("quota_in"):
                    self.step = "borrowed_quota"
                    link = quota.vault.get_secret(state["quota_in"], "quota", "in")
                    host, port, token, fingerprint = parse_link(link)
                    quota.remote = {"host": host, "port": port, "token": token, "upstream": f"https://{host}:{port}/v1",
                                    "opener": pinned_opener(host, port, fingerprint), "credential_id": state["quota_in"]}
                if state.get("exit_in"):
                    self.step = "borrowed_network"
                    link = quota.vault.get_secret(state["exit_in"], "exit", "in")
                    row = next(row for row in quota.vault.rows() if row["id"] == state["exit_in"])
                    egress.peer_opener = egress.opener_factory(link, client_id=quota.vault.client_id,
                                                              resume_snapshot=row.get("snapshot", {}))
                    egress.mode, egress.peer_link, egress.peer_id = "peer", link, state["exit_in"]
                egress.local_link, egress.callback_link = state.get("local_link", ""), state.get("callback_link", "")
                self._record("staged")
            except Exception as exc:
                self._record("stage_failed", exc)
                self.finish(False)
                raise

    def activate(self):
        with self.guard:
            if self.phase not in {"prepared", "yielded"}:
                return False
            for listener in self.listeners:
                listener.start()
            self.server.sharing.owns_provider_state = True
            was_yielded = self.phase == "yielded"
            self.phase = "active" if self.mode == "import" else "prepared"
            # Start accepting first; both handles refer to the same kernel
            # listening socket. Existing TLS streams stay with their worker.
            if self.mode == "import" or was_yielded:
                control_worker(self.server.data_dir, self.peer, "sharing_yield", ticket=self.ticket,
                               peer=self.server.worker_id)
            self._record("accepting")
            return True

    def yield_accept(self, ticket, peer):
        with self.guard:
            if ticket != self.ticket or peer != self.peer or self.phase not in {"prepared", "active", "yielded"}:
                raise ValueError("共享交接已变更。")
            for listener in self.listeners:
                listener.pause()
            self.server.sharing.owns_provider_state = False
            self.phase = "yielded"
            self._record("yielded")

    def finish(self, committed):
        with self.guard:
            if self.phase not in {"prepared", "active", "yielded"}:
                return
            if not committed and self.mode == "export":
                if self.phase == "yielded":
                    self.activate()
                self.server.sharing.owns_provider_state = True
            elif (committed and self.mode == "export") or (not committed and self.mode == "import"):
                for listener in self.listeners:
                    listener.pause()
                    listener.server_close()
                self.server.sharing.owns_provider_state = False
                if self.mode == "import":
                    # Do not retain a machine-wide role after a failed stage.
                    self.server.sharing.role.close()
            self.phase = "complete" if committed else "cancelled"
            self._record(self.phase)


def prepare_upgrade(data_dir, source, target):
    state = control_worker(data_dir, source, "sharing_export", target=target)
    try:
        control_worker(data_dir, target, "sharing_stage", snapshot=state)
    except Exception:
        control_worker(data_dir, source, "sharing_finish", committed=False, ticket=state["ticket"])
        raise
    return state["ticket"]


def finish_upgrade(data_dir, source, target, ticket):
    active = json.loads((data_dir / "router-state.json").read_text(encoding="utf8"))["active"]
    if active not in {source, target}:
        raise ValueError("共享交接目标已变更。")
    committed = active == target
    # Restore the old acceptor before discarding a candidate on rollback.
    control_worker(data_dir, source, "sharing_finish", committed=committed, ticket=ticket)
    control_worker(data_dir, target, "sharing_finish", committed=committed, ticket=ticket)
