"""Forward saved SSH Codex providers through the Windows optimizer."""
from __future__ import annotations

import base64
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

import tomlkit

from .config_link import validate_upstream
from .connection_plan import ConfigSnapshot
from .connection_policy import native_compression
from .linux_images import LinuxImageTarget, linux_image_edits, probe_image_directory
from .network_diagnostics import safe_text
from .provider_defaults import API_UPSTREAM
from .provider_migration import provider_selection_changed
from .providers import _get, _put, prepare_provider_files
from .remote_connections import SshControl
from .router import RouterHandler, atomic_json

CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
TUNNEL_TOKEN = re.compile(r"[0-9a-f]{48}")
RETRY_SECONDS = 12
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def original_remote_upstream(value, linked, previous):
    upstream = validate_upstream(value)
    url = urlsplit(upstream)
    if url.hostname not in LOOPBACK:
        return upstream
    parts = url.path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["relay", previous.get("token")] and parts[2] == "p":
        original = previous.get("routes", {}).get(parts[3])
        if not original:
            raise ValueError("SSH 中转路由缺少原上游记录，请从备份恢复。")
        return validate_upstream(original)
    if parts[:2] != ["relay", linked.get("legacy_token")]:
        return upstream
    if len(parts) == 4 and parts[2] == "p":
        original = linked.get("legacy_routes", {}).get(parts[3])
    else:
        original = linked.get("legacy_upstream") if len(parts) == 2 else None
    if not original:
        raise ValueError("旧中转的路由缺少原上游记录，远程配置未修改。")
    return validate_upstream(original)


def check_remote_upstream(value):
    upstream = validate_upstream(value)
    if urlsplit(upstream).hostname in LOOPBACK:
        raise ValueError("远程 provider 指向服务器本地服务，Windows 中转无法直接访问。")
    return upstream


def config_changes(files, linked, previous, endpoint, *, retry_limit=None):
    """Run the shared provider policy on remote snapshots."""
    routes = dict(previous.get("routes", {}))
    documents = {name: tomlkit.parse((raw or b"").decode("utf-8-sig")) for name, raw in files.items()}

    for name, document in documents.items():
        # Recover the pre-link values only where our previous writes still match.
        for field in managed_fields(previous, name):
            present, value = _get(document, field["keys"])
            if present and value == field["applied"]:
                _put(document, field["keys"], field["before"], field["present"])
                for parent in reversed(field.get("added_tables", [])):
                    _, table = _get(document, parent)
                    if isinstance(table, dict) and not table and not table.as_string().strip():
                        _put(document, parent, None, False)
        if not endpoint and files[name] is not None:
            native_compression(document, True)

    updated = {name: tomlkit.dumps(document) for name, document in documents.items()}
    fields, aliases, warnings = {}, set(), []
    if endpoint:
        def route(_path, _provider, upstream):
            identifier = next((key for key, existing in routes.items() if existing == upstream), None)
            if identifier is None:
                identifier = secrets.token_hex(12)
            routes[identifier] = upstream
            return f"{endpoint}/p/{identifier}"

        native = API_UPSTREAM if linked.get("api_key") else CHATGPT_UPSTREAM
        names = ["config.toml", *(name for name in documents if name != "config.toml")]
        sources = tuple(ConfigSnapshot(PurePosixPath(name), updated[name].encode("utf8")) for name in names)
        records = {source.path: [] for source in sources}
        plans, aliases, warnings, _ = prepare_provider_files(
            sources, native, records, lambda value, _: original_remote_upstream(value, linked, previous), route,
            retry_limit=retry_limit, check_upstream=check_remote_upstream)
        updated.update((edit.source.path.name, edit.updated) for edit in plans)
        fields = {path.name: records[path] for path in records}
    edits = {}
    for name, rendered in updated.items():
        raw = files[name] or b""
        original = raw.decode("utf-8-sig")
        if rendered != original:
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            edits[name] = bom + rendered.encode("utf-8")
    return edits, routes, fields, aliases, warnings


def managed_fields(record, name):
    return record.get("fields", {}).get(name, []) + record.get("previous_fields", {}).get(name, [])


def provider_policy_pending(record, policy):
    return record.get("provider_policy") != policy and "previous_fields" not in record


def changes_provider(files, edits):
    return any(provider_selection_changed((files[name] or b"").decode("utf-8-sig"), raw.decode("utf-8-sig"))
               for name, raw in edits.items())


class TunnelHandler(RouterHandler):
    def _handle(self):
        self.connection.settimeout(600)
        try:
            path = urlsplit(self.path)
        except ValueError:
            self.reject(400, {"error": "invalid_request_path"})
            return
        parts = path.path.split("/", 5)
        if (path.scheme or path.netloc or path.fragment or len(parts) < 4 or parts[1] != "relay"
                or not TUNNEL_TOKEN.fullmatch(parts[2]) or self.headers.get("Origin")):
            self.reject(404, {"error": "not_found"})
            return
        token = parts[2]
        record = self.server.relays.find(token)
        if not record:
            self.reject(404, {"error": "not_found"})
            return
        if self.command == "GET" and parts[3:] == ["ready"] and "proof" in record:
            raw = record["proof"].encode("ascii")
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
            return
        if len(parts) != 6 or parts[3] != "p" or parts[4] not in record.get("routes", {}):
            self.reject(404, {"error": "not_found"})
            return
        suffix = unquote(parts[5])
        if not suffix or suffix.startswith("/") or "\\" in suffix or ".." in suffix.split("/"):
            self.reject(400, {"error": "invalid_request_path"})
            return
        target = self.server.relays.endpoint()
        self.path = urlsplit(target).path + "/ssh/" + token + "/p/" + parts[4] + "/" + parts[5]
        if path.query:
            self.path += "?" + path.query
        try:
            self._forward(self.path.partition("?")[0])
        except ValueError:
            self.reject(503, {"error": "ssh_relay_reconnecting"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class TunnelGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, relays):
        self.relays = relays
        self.guard = threading.Lock()
        self.active = 0
        self.stopping = False
        super().__init__(("127.0.0.1", 0), TunnelHandler)
        threading.Thread(target=self.serve_forever, name="ssh-relay-ingress", daemon=True).start()

    def acquire(self, relay):
        with self.guard:
            if self.stopping:
                raise ValueError("SSH ingress closing")
            self.active += 1
            return "", {"port": urlsplit(self.relays.endpoint()).port}

    def release(self, identity, relay):
        with self.guard:
            self.active -= 1

    def drain(self):
        with self.guard:
            if self.active:
                return False
            self.stopping = True
            return True


class RemoteRelays:
    def __init__(self, directory, endpoint, *, ssh=None, start=None, image_settings=None, retry_limit=None):
        self.directory = Path(directory)
        self.endpoint = endpoint
        self.ssh = ssh or SshControl()
        self.start = start or subprocess.Popen
        self.gateway = None
        self.processes = {}
        self.current_accounts = {}
        self.image_settings = image_settings
        self.retry_limit = retry_limit or (lambda: None)
        self.image_targets = {}
        self.retry_at = {}
        self.guard = threading.RLock()
        self.closed = False

    @staticmethod
    def key(host):
        identity = (host.id, host.destination, host.port, host.identity)
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def records(self):
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf8"))
                if TUNNEL_TOKEN.fullmatch(record["token"]):
                    yield path.stem, record
            except (OSError, TypeError, ValueError, KeyError):
                continue

    def find(self, token):
        return next((record for _, record in self.records() if record["token"] == token), None)

    def route(self, token, identifier):
        for key, record in self.records():
            if record["token"] == token:
                return record.get("routes", {}).get(identifier), key
        return None, None

    def needs_resume(self, host=None):
        device = self.key(host) if host else None
        now = time.monotonic()
        policy = self._provider_policy()
        with self.guard:
            if self.closed:
                return False
            for key, record in self.records():
                host_key = key.partition("-")[0]
                if (device is not None and device != host_key) or self.current_accounts.get(host_key, key) != key:
                    continue
                if (record.get("enabled") and self.retry_at.get(host_key, 0) <= now
                        and (key not in self.processes or self.processes[key].poll() is not None
                             or provider_policy_pending(record, policy))):
                    return True
            return False

    def _save(self, key, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / (key + ".json"), record)

    def _provider_policy(self):
        return {"version": 2, "retry_limit": self.retry_limit()}

    def _account_record(self, device, account_key, account):
        records = dict(self.records())
        if account_key in records:
            return account_key, records[account_key]
        legacy = records.get(device, {})
        if legacy.get("home") == account["home"] and legacy.get("uid", account["uid"]) == account["uid"]:
            # Pre-account records identified the login by its resolved config directory.
            if "uid" not in legacy:
                legacy["uid"] = account["uid"]
                self._save(device, legacy)
            return device, legacy
        return account_key, {}

    def _call(self, host, action, **options):
        result = self.ssh(host, "tunnel_" + action, **options)
        if result.get("error") and (action != "reconnect" or "state" not in result):
            raise ValueError(result["error"])
        return result

    def image_target(self, key):
        with self.guard:
            entry = self.image_targets.get(key)
            if entry and key in self.current_accounts.values():
                return entry[2]
        return None

    def _images(self, host, key, record, files, account, active, action):
        if not self.image_settings or not record:
            return None
        mode, data_dir = self.image_settings(host.id) if active else ("native", "")
        desired = mode, data_dir
        with self.guard:
            cached = self.image_targets.get(key)
        if active and action not in {"activate", "images"} and cached and cached[0] == desired:
            return cached[1]
        if not active and action != "restore":
            return None

        def send(operation, **options):
            return self._call(host, operation, **account, **options)

        status = {"data_dir": data_dir, "directory": "", "error": ""}
        target = None
        if active:
            try:
                status["directory"] = probe_image_directory(send, data_dir)
            except (OSError, ValueError) as exc:
                status["error"] = safe_text(str(exc), limit=240)
        try:
            if mode != "optimized" or status["directory"]:
                previous = [*record.get("image_pending", []), *record.get("image_fields", [])]
                edit, fields, uninstall, files_changed = linux_image_edits(files["config.toml"], previous, mode, send)
                if files_changed:
                    record["reload_required"] = True
                if edit is not None:
                    record["image_pending"] = fields
                    self._save(key, record)
                    expected = {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                for name, raw in files.items()}
                    changes = {"config.toml": base64.b64encode(edit).decode("ascii")}
                    outcome = send("write", expected=expected, updates=changes)
                    files["config.toml"] = edit
                    record["reload_required"] = bool(outcome["changed"] or record.get("reload_required"))
                if files_changed or edit is not None or fields or previous:
                    record["image_fields"] = fields
                    record.pop("image_pending", None)
                    self._save(key, record)
                if mode == "optimized":
                    target = LinuxImageTarget(status["directory"], send)
                elif uninstall:
                    send("image_uninstall")
        except (OSError, ValueError) as exc:
            status["error"] = safe_text(str(exc), limit=240)
        with self.guard:
            if active:
                self.image_targets[key] = (desired, status, target)
            else:
                self.image_targets.pop(key, None)
        return status

    def _drop(self, key):
        process = self.processes.pop(key, None)
        if process:
            process.stdin.close()
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            process.stderr.close()

    def _ensure(self, host, key, record):
        with self.guard:
            if self.closed:
                raise ValueError("本机中转正在退出。")
            if key in self.processes and self.processes[key].poll() is None:
                return
            self._drop(key)
            if self.gateway is None:
                self.gateway = TunnelGateway(self)
            local = self.gateway.server_port
            port = record["port"]
            arguments = SshControl.arguments(host, fresh=True) + [
                "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{port}:127.0.0.1:{local}",
                "--", host.destination, "exec cat >/dev/null"]
            # Closing the local stdin also releases the remote forward after an
            # unexpected worker exit. The SSH host runs only its existing sshd.
            process = self.start(arguments, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.processes[key] = process
        try:
            probe = self._call(host, "probe", home=record["home"], uid=record["uid"], port=port, token=record["token"])
            proof = probe.get("proof")
            if not isinstance(proof, str) or not secrets.compare_digest(proof, record["proof"]):
                raise ValueError("无法建立 SSH 请求转发，请检查 SSH 是否允许远程端口转发。")
            if process.poll() is not None:
                # An old worker can still be serving the tunnel it is draining.
                # Confirm the same local proof, then retry taking over shortly.
                with self.guard:
                    self._drop(key)
                    self.retry_at[self.key(host)] = time.monotonic() + 3
        except Exception:
            with self.guard:
                self._drop(key)
            raise

    def drain(self):
        with self.guard:
            return self.gateway is None or self.gateway.drain()

    def close(self):
        with self.guard:
            self.closed = True
            for key in list(self.processes):
                self._drop(key)
            gateway = self.gateway
        if gateway:
            gateway.shutdown()
            gateway.server_close()

    def __call__(self, host, action, origin=""):
        device = self.key(host)
        with self.guard:
            if self.closed:
                raise ValueError("本机中转正在退出。")
            self.retry_at[device] = time.monotonic() + RETRY_SECONDS
        snapshot = self._call(host, "snapshot", native=action != "reconnect")
        account = {"home": snapshot["home"], "uid": snapshot["uid"]}
        account_hash = hashlib.sha256(json.dumps(account, sort_keys=True).encode()).hexdigest()[:24]
        account_key = f"{device}-{account_hash}"
        with self.guard:
            key, record = self._account_record(device, account_key, account)
            previous_account = self.current_accounts.get(device)
            if previous_account != key:
                self.image_targets.pop(previous_account, None)
            self.current_accounts[device] = key
        files = {name: base64.b64decode(raw, validate=True) if raw is not None else None
                 for name, raw in snapshot["files"].items()}
        active = False
        for name, raw in files.items():
            managed = managed_fields(record, name)
            if managed:
                document = tomlkit.parse((raw or b"").decode("utf-8-sig"))
                active |= any(_get(document, field["keys"]) == (True, field["applied"])
                              for field in managed if isinstance(field["applied"], str))
        changed = False
        warnings, migration = [], {}
        policy = self._provider_policy()
        reconcile = (action is None and active and record.get("enabled")
                     and provider_policy_pending(record, policy))
        if reconcile:
            # Restore service for the old config even if it cannot be migrated.
            self._ensure(host, key, record)
        images = self._images(host, key, record, files, account, False, action) if action == "restore" else None
        if reconcile or action in {"activate", "restore"}:
            if action != "restore":
                if not record:
                    record = {"token": secrets.token_hex(24), "port": 49152 + secrets.randbelow(16384),
                              "proof": secrets.token_hex(24), **account}
                endpoint = f"http://127.0.0.1:{record['port']}/relay/{record['token']}"
            else:
                endpoint = ""
            if record:
                edits, routes, fields, aliases, warnings = config_changes(
                    files, snapshot, record, endpoint, retry_limit=policy["retry_limit"])
                if endpoint:
                    # Route and recovery details must exist before any request
                    # can use the new configuration.
                    prior = dict(record)
                    record.update(enabled=True, routes=routes, fields=fields)
                    if edits:
                        # Keep both candidates if the SSH write result becomes
                        # uncertain. Neither is replayed automatically.
                        record["previous_fields"] = {name: managed_fields(prior, name) for name in files}
                    self._save(key, record)
                    if not reconcile:
                        try:
                            self._ensure(host, key, record)
                        except (OSError, ValueError):
                            self._save(key, prior)
                            raise
                if edits or aliases:
                    expected = {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                for name, raw in files.items()}
                    updates = {name: base64.b64encode(raw).decode("ascii") for name, raw in edits.items()}
                    outcome = self._call(host, "write", expected=expected, updates=updates,
                                         api_key=snapshot.get("api_key"), provider_aliases=sorted(aliases),
                                         require_reload=changes_provider(files, edits), **account)
                    migration = outcome.get("session_migration", {})
                    changed = bool(outcome["changed"] or migration.get("changed"))
                    files.update(edits)
                active = bool(endpoint)
                if endpoint:
                    waiting = bool(migration.get("waiting_for_reload") or migration.get("warnings"))
                    record["reload_required"] = bool(changed or waiting or record.get("reload_required"))
                    record["provider_migration_waiting"] = waiting
                    if waiting:
                        record.pop("provider_policy", None)
                    else:
                        record["provider_policy"] = policy
                else:
                    record.update(enabled=False, fields={}, reload_required=bool(changed or record.get("reload_required")))
                record.pop("previous_fields", None)
                self._save(key, record)
        elif active:
            self._ensure(host, key, record)
        if action != "restore":
            images = self._images(host, key, record, files, account, active, action)
        result = {"state": "connected" if active else "disconnected", "can_connect": True, "account_key": account_key,
                  "can_disconnect": bool(active), "reload_required": bool(
                      record.get("reload_required") or active and "previous_fields" in record)}
        if warnings or migration.get("warnings"):
            result["provider_warnings"] = warnings + migration.get("warnings", [])
        if migration.get("warnings"):
            result["error"] = "远程会话的 provider 索引更新未完成，将在后台重试。"
        if images is not None:
            result["image_storage"] = images
        if action == "reconnect":
            options = {}
            if record.get("provider_migration_waiting"):
                endpoint = f"http://127.0.0.1:{record['port']}/relay/{record['token']}"
                edits, _, _, aliases, _ = config_changes(files, snapshot, record, endpoint,
                                                        retry_limit=policy["retry_limit"])
                if not changes_provider(files, edits):
                    options = {"provider_aliases": sorted(aliases),
                               "expected": {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                            for name, raw in files.items()}, "api_key": snapshot.get("api_key")}
            reconnect = self._call(host, "reconnect", **options, **account)
            result["codex_reconnect"] = reconnect
            result["codex_restart"] = reconnect["codex_restart"]
            finalized = not record.get("provider_migration_waiting") or "session_migration" in reconnect
            if not finalized and reconnect["state"] == "reloaded" and not reconnect.get("warning"):
                reconnect["warning"] = "远程会话的 provider 迁移尚未完成，已保留待生效提醒。"
            if record and reconnect["state"] == "reloaded" and not reconnect.get("warning") and finalized:
                if "session_migration" in reconnect:
                    record.pop("provider_migration_waiting", None)
                record["reload_required"] = result["reload_required"] = False
                if active:
                    self._save(key, record)
                else:
                    with self.guard:
                        self._drop(key)
                    (self.directory / (key + ".json")).unlink(missing_ok=True)
        else:
            result["codex_restart"] = snapshot["codex_restart"]
        return result
