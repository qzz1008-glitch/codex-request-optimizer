"""Discover and control Codex's saved SSH hosts off the UI request threads."""
from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

from .native_state import managed_remote_connections, read_global_state
from .network_diagnostics import safe_text
from .remote_control import SCRIPT, RESULT_PREFIX

MAX_TARGETS = 64
PROBE_TTL = 30


@dataclass(frozen=True)
class Host:
    id: str
    name: str
    destination: str
    port: int | None = None
    identity: str | None = None


def _destination(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[\w.@:[\]-]{1,255}", value)) and not value.startswith("-")


def ssh_aliases(config, *, config_hash=None):
    """Only concrete Host entries, including bounded OpenSSH Include files."""
    aliases, visited = [], set()

    def read(path):
        path = path.expanduser().resolve()
        if path in visited or len(visited) >= 16:
            return
        visited.add(path)
        try:
            if path.stat().st_size > 1024 * 1024:
                return
            contents = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return
        if config_hash is not None:
            config_hash.update(json.dumps((str(path), contents)).encode())
        for line in contents.splitlines():
            try:
                fields = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if not fields:
                continue
            key = fields[0].lower()
            if key == "host":
                aliases.extend(value for value in fields[1:] if _destination(value) and value not in aliases)
            elif key == "include":
                for value in fields[1:]:
                    pattern = Path(value).expanduser()
                    if not pattern.is_absolute():
                        pattern = config.parent / pattern
                    for child in sorted(glob.glob(str(pattern)))[:16]:
                        read(Path(child))

    read(config)
    return aliases[:MAX_TARGETS]


def saved_hosts(codex_home, ssh_config, *, config_hash=None):
    hosts, destinations = [], set()
    try:
        state = read_global_state(codex_home)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("无法读取 Codex 已保存的服务器列表。") from exc
    for row in managed_remote_connections(state):
        if not isinstance(row, dict):
            continue
        identity = row.get("hostId", "")
        alias = row.get("alias")
        alias = alias.strip() if isinstance(alias, str) else ""
        destination = alias or row.get("hostname")
        if not isinstance(identity, str) or not identity.startswith("remote-ssh-") or not _destination(destination):
            continue
        # Match Codex's SSH argv: an alias owns its OpenSSH options; only
        # destinations without an alias receive explicit -i / -p options.
        port = None if alias else row.get("sshPort")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            continue
        keyfile = None if alias else row.get("identity")
        if keyfile is not None and (not isinstance(keyfile, str) or any(c in keyfile for c in "\r\n\0")):
            continue
        key = (destination, port, keyfile)
        if identity in {host.id for host in hosts} or key in destinations:
            continue
        name = str(row.get("displayName") or destination)[:160]
        hosts.append(Host(identity, name, destination, port, keyfile))
        destinations.add(key)
        if len(hosts) >= MAX_TARGETS:
            break
    referenced = set(state.get("remote-connection-analytics-id-by-host-id", {})) if isinstance(state, dict) and isinstance(state.get("remote-connection-analytics-id-by-host-id"), dict) else set()
    projects = state.get("remote-projects", []) if isinstance(state, dict) else []
    for project in projects if isinstance(projects, list) else []:
        if isinstance(project, dict) and isinstance(project.get("hostId"), str):
            referenced.add(project["hostId"])
    if isinstance(state, dict):
        for key, value in state.items():
            if ":selected-remote-host-id:" in key and isinstance(value, str):
                referenced.add(value)
            if ":remote-connection-auto-connect-by-host-id:" in key and isinstance(value, dict):
                referenced.update(value)
    for alias in ssh_aliases(ssh_config, config_hash=config_hash):
        if any(host.destination == alias for host in hosts):
            continue
        identity = "remote-ssh-discovered:" + quote(alias, safe="-_.!~*'()")
        # An SSH config also contains Git hosts and unrelated machines. Only
        # hosts actually saved/used in Codex belong to a bulk Codex operation.
        if identity not in referenced or alias == "wsl-local":
            continue
        hosts.append(Host(identity, alias, alias))
        if len(hosts) >= MAX_TARGETS:
            break
    return hosts


class SshControl:
    @staticmethod
    def arguments(host, *, fresh=False):
        executable = shutil.which("ssh")
        if not executable:
            raise ValueError("未找到 OpenSSH 客户端。")
        arguments = [executable, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2"]
        if fresh:
            arguments += ["-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                          "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ForkAfterAuthentication=no"]
        if host.port is not None:
            arguments += ["-p", str(host.port)]
        if host.identity:
            arguments += ["-i", str(Path(host.identity).expanduser())]
        return arguments

    def __call__(self, host, action, origin="", **options):
        tunnel = bool(action and action.startswith("tunnel_"))
        arguments = self.arguments(host, fresh=tunnel)
        if tunnel:
            arguments += ["-o", "ClearAllForwardings=yes"]
        arguments += ["--", host.destination, "exec \"${SHELL:-/bin/sh}\" -lc 'cd / && exec python3 -I -'"]
        payload = json.dumps({"action": action, "origin": origin, **options}, ensure_ascii=True)
        script = "INPUT = " + repr(payload) + "\n" + SCRIPT
        try:
            result = subprocess.run(arguments, input=script.encode(), capture_output=True,
                timeout=(12 if action == "tunnel_labels" else
                         240 if action in {"reconnect", "tunnel_reconnect"} else 90 if action else 30),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ValueError("操作结果未确认，请刷新状态后检查。" if action else "SSH 状态检查超时。") from None
        except OSError:
            raise ValueError("无法启动 OpenSSH 客户端。") from None
        if result.returncode:
            detail = safe_text(result.stderr[-2000:].decode("utf-8", errors="replace"), (), 1000)
            raise ValueError("SSH 执行失败：" + (detail or str(result.returncode)))
        if len(result.stdout) > 1024 * 1024:
            raise ValueError("服务器返回内容过大，未确认接入状态。")
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        try:
            value = json.loads(next(line[len(RESULT_PREFIX):] for line in reversed(lines) if line.startswith(RESULT_PREFIX)))
        except (StopIteration, ValueError):
            raise ValueError("服务器未返回接入状态，请检查 SSH 登录及系统的 Python 3。") from None
        if not isinstance(value, dict):
            raise ValueError("服务器返回的接入状态无效。")
        return value


class Connections:
    def __init__(self, codex_home, local_profile, local_apply, *, transport=None, ssh_config=None, cache_path=None):
        self.codex_home = Path(codex_home)
        self.ssh_config = Path(ssh_config) if ssh_config else Path.home() / ".ssh" / "config"
        self.local_profile, self.local_apply = local_profile, local_apply
        self.transport = transport or SshControl()
        self.guard = threading.RLock()
        self.slots = threading.Semaphore(4)
        self.entries = {}
        self.closed = False
        self.resume_after = 0
        self.revision = ""
        self.cache_path = Path(cache_path) if cache_path else None
        self.origins = {}
        if self.cache_path:
            try:
                if self.cache_path.stat().st_size <= 65536:
                    value = json.loads(self.cache_path.read_text(encoding="utf8"))
                    if isinstance(value, dict):
                        self.origins = value
            except (OSError, ValueError):
                pass  # Only a port hint; live status remains authoritative.

    def close(self):
        with self.guard:
            self.closed = True
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def resume(self):
        needs_resume = getattr(self.transport, "needs_resume", None)
        if not needs_resume:
            return
        with self.guard:
            now = time.monotonic()
            if self.closed or now < self.resume_after:
                return
            self.resume_after = now + 3
            if not needs_resume():
                return
            try:
                self._discover()
            except ValueError:
                return
            for entry in self.entries.values():
                host = entry["host"]
                if host and not entry.get("busy") and needs_resume(host):
                    self._launch(entry)

    def _local(self):
        try:
            profile = self.local_profile()
            error = profile.get("config_error", "")
            return {"state": "error" if error else "connected" if profile.get("active") else "disconnected",
                    "error": str(error), "can_connect": not bool(error),
                    "can_disconnect": bool(profile.get("can_restore") or profile.get("active"))}
        except Exception as exc:
            return {"state": "error", "error": type(exc).__name__, "can_connect": False, "can_disconnect": False}

    def _discover(self):
        config_hash = hashlib.sha256()
        hosts = saved_hosts(self.codex_home, self.ssh_config, config_hash=config_hash)
        ssh_revision = config_hash.hexdigest()
        source = [(host.id, host.name, host.destination, host.port, host.identity) for host in hosts]
        self.revision = hashlib.sha256(json.dumps((source, ssh_revision)).encode()).hexdigest()
        current = {host.id for host in hosts} | {"local"}
        for identity in list(self.entries):
            if identity not in current and not self.entries[identity].get("busy"):
                del self.entries[identity]
        for host in [None, *hosts]:
            identity = host.id if host else "local"
            entry = self.entries.setdefault(identity, {"id": identity, "state": "unknown", "checked": 0})
            if entry.get("host") is not None and (entry["host"] != host or entry.get("ssh_revision") != ssh_revision):
                if entry.get("busy"):
                    continue
                entry.clear()
                entry.update(id=identity, state="unknown", checked=0)
            hint = self.origins.get(identity, {})
            if host and isinstance(hint, dict) and hint.get("destination") == host.destination and hint.get("port") == host.port:
                entry.setdefault("origin", hint.get("origin", ""))
            entry.update(host=host, ssh_revision=ssh_revision, name=host.name if host else "本机", kind="ssh" if host else "local")

    def _remember_origin(self, host, result):
        origin = result.get("origin")
        if not isinstance(origin, str) or not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", origin):
            return
        value = {"destination": host.destination, "port": host.port, "origin": origin}
        if self.origins.get(host.id) == value:
            return
        self.origins[host.id] = value
        self.origins = {key: value for key, value in self.origins.items() if key in self.entries}
        if self.cache_path:
            staging = self.cache_path.with_name(self.cache_path.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                staging.write_text(json.dumps(self.origins, ensure_ascii=True), encoding="utf8")
                staging.replace(self.cache_path)
            except OSError:
                pass
            finally:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass

    def _launch(self, entry, action=None):
        entry.update(busy=True, action=action or "check", error="")
        if action:
            entry.pop("action_error", None)
        if action == "reconnect":
            entry.pop("codex_reconnect", None)
        host, identity = entry["host"], entry["id"]
        origin = entry.get("origin", "")

        def run():
            result = {}
            try:
                with self.slots:
                    if self.closed:
                        return
                    if host:
                        result = self.transport(host, action, origin)
                    else:
                        # The existing connection flow owns idempotency, WSL
                        # reconciliation and the saved auto-connect intent.
                        details = self.local_apply(action == "activate")
                        result = {**self._local(), "reload_required": bool(details.get("reload_required")),
                                  "local_result": details}
                    if result.get("state") not in {"connected", "disconnected", "error"}:
                        raise ValueError("服务器返回的接入状态无效。")
                    if action == "reconnect" and not isinstance(result.get("codex_reconnect"), dict):
                        result["codex_reconnect"] = {"state": "unconfirmed", "error": "服务器未确认 Codex 重载结果，请刷新状态后检查。"}
                    if action in {"activate", "restore"} and result["state"] != ("connected" if action == "activate" else "disconnected"):
                        result["error"] = result.get("error") or "接入配置未完全更新，请查看设备状态。"
            except Exception as exc:
                detail = safe_text(str(exc), (), 1500)
                result = {"codex_reconnect": {"state": "unconfirmed", "error": detail}} if action == "reconnect" else {"state": "error", "error": detail}
            finally:
                with self.guard:
                    if host:
                        self._remember_origin(host, result)
                        if result.get("account_key") and result["account_key"] != entry.get("account_key"):
                            entry.pop("codex_reconnect", None)
                    entry.update(result, busy=False, checked=time.monotonic())
                    if action:
                        entry["completed_action"] = action
                        entry["action_error"] = result.get("error", "")
        threading.Thread(target=run, name="connection-" + identity[:24], daemon=True).start()

    def snapshot(self, *, refresh=False):
        with self.guard:
            try:
                self._discover()
                catalog_error = ""
            except ValueError as exc:
                catalog_error = str(exc)
                self.entries.setdefault("local", {"id": "local", "name": "本机", "kind": "local", "host": None})
            rows = []
            for entry in self.entries.values():
                if entry["id"] == "local":
                    if not entry.get("busy"):
                        entry.update(self._local())
                        if entry.get("action_error"):
                            entry["error"] = entry["action_error"]
                elif not entry.get("busy") and (refresh or time.monotonic() - entry.get("checked", 0) >= PROBE_TTL) and not self.closed:
                    self._launch(entry)
                rows.append({key: value for key, value in entry.items() if key not in {
                    "host", "origin", "checked", "local_result", "action_error", "ssh_revision", "account_key"}})
            return {"targets": rows, "error": catalog_error, "revision": self.revision}

    def apply(self, identities, action, *, revision=None):
        if action not in {"activate", "restore", "reconnect"} or not isinstance(identities, list) or not identities or len(identities) > MAX_TARGETS + 1:
            raise ValueError("接入操作或设备列表无效。")
        if any(not isinstance(identity, str) for identity in identities) or len(set(identities)) != len(identities):
            raise ValueError("接入设备列表无效。")
        with self.guard:
            if self.closed:
                raise ValueError("服务正在退出，请稍后重试。")
            self._discover()
            if revision is not None and revision != self.revision:
                raise ValueError("设备列表已变化，请重新确认影响范围。")
            if any(identity not in self.entries for identity in identities):
                raise ValueError("设备列表已变化，请重新确认影响范围。")
            if action == "reconnect" and (len(identities) != 1 or identities[0] == "local"):
                raise ValueError("请选择一台服务器重新连接 Codex。")
            if any(self.entries[identity].get("busy") for identity in identities):
                raise ValueError("所选设备正在检查或修改接入配置，请稍后重试。")
            for identity in identities:
                entry = self.entries[identity]
                entry.pop("completed_action", None)
                if action != "reconnect":
                    entry.pop("reload_required", None)
                entry["operation_id"] = secrets.token_hex(8)
                self._launch(entry, action)
            return {"accepted": {identity: self.entries[identity]["operation_id"] for identity in identities}}
