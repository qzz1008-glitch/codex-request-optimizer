"""Forward saved SSH Codex providers through the Windows optimizer."""
from __future__ import annotations

import base64
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

import tomlkit

from .config_link import validate_upstream
from .configuration_layers import merge_layers
from .linux_images import LinuxImageTarget, linux_image_edits, probe_image_directory
from .network_diagnostics import safe_text
from .provider_defaults import API_UPSTREAM, provider_upstream
from .providers import _get, _put
from .remote_connections import SshControl
from .router import RouterHandler, atomic_json

CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
TUNNEL_TOKEN = re.compile(r"[0-9a-f]{48}")
RETRY_SECONDS = 12


def config_changes(files, linked, previous, endpoint):
    """Change standard HTTP fields while keeping remote provider IDs intact."""
    routes = dict(previous.get("routes", {}))
    fields = {}
    edits = {}
    documents = {name: tomlkit.parse((raw or b"").decode("utf-8-sig")) for name, raw in files.items()}
    base = documents["config.toml"]
    root_providers = tomlkit.parse((files["config.toml"] or b"").decode("utf-8-sig")).get("model_providers", {})
    native = API_UPSTREAM if linked.get("api_key") else CHATGPT_UPSTREAM
    default_provider = base.get("model_provider", "openai")
    selected = {document.get("model_provider", default_provider) for document in documents.values()}
    selected.update(profile["model_provider"] for document in documents.values()
                    for profile in document.get("profiles", {}).values() if "model_provider" in profile)

    for name, document in documents.items():
        old_fields = managed_fields(previous, name)
        # Recover the pre-link values only where our previous writes still match.
        for field in old_fields:
            present, value = _get(document, field["keys"])
            if present and value == field["applied"]:
                _put(document, field["keys"], field["before"], field["present"])
                for parent in reversed(field.get("added_tables", [])):
                    _, table = _get(document, parent)
                    if isinstance(table, dict) and not table and not table.as_string().strip():
                        _put(document, parent, None, False)
        if endpoint:
            records = fields.setdefault(name, [])

            def change(keys, value):
                present, before = _get(document, keys)
                if present and before == value:
                    return
                added_tables = [keys[:index] for index in range(1, len(keys)) if not _get(document, keys[:index])[0]]
                records.append({"keys": keys, "present": present, "before": before,
                                "applied": value, "added_tables": added_tables})
                _put(document, keys, value)

            def route(keys, value):
                upstream = validate_upstream(value)
                url = urlsplit(upstream)
                if url.hostname in {"localhost", "127.0.0.1", "::1"}:
                    parts = url.path.strip("/").split("/")
                    if len(parts) < 2 or parts[:2] != ["relay", linked.get("legacy_token")]:
                        raise ValueError("远程 provider 指向服务器本地服务，Windows 中转无法直接访问。")
                    if len(parts) == 4 and parts[2] == "p":
                        original = linked.get("legacy_routes", {}).get(parts[3])
                    elif len(parts) == 2:
                        original = linked.get("legacy_upstream")
                    else:
                        original = None
                    if not original:
                        raise ValueError("旧中转的路由缺少原上游记录，远程配置未修改。")
                    upstream = validate_upstream(original)
                    if urlsplit(upstream).scheme != "https":
                        raise ValueError("旧中转的原上游仅在服务器本地可访问。")
                    # The old service may already have been uninstalled. A
                    # later disconnect must restore its upstream, not its URL.
                    _put(document, keys, upstream)
                identifier = next((key for key, existing in routes.items() if existing == upstream), None)
                if identifier is None:
                    identifier = secrets.token_hex(12)
                routes[identifier] = upstream
                change(keys, f"{endpoint}/p/{identifier}")

            if name == "config.toml" or "openai_base_url" in document:
                try:
                    route(["openai_base_url"], document.get("openai_base_url") or native)
                except ValueError:
                    if "openai" in selected:
                        raise
            # Use the pre-link root provider for inheritance even if a profile
            # declares some of the same fields.
            for provider_id, provider in document.get("model_providers", {}).items():
                inherited = root_providers.get(provider_id, {}) if document is not base else {}
                merged = merge_layers(inherited, provider)
                try:
                    if merged.get("wire_api", "responses") != "responses":
                        raise ValueError("远程所选 provider 不使用 Responses API。")
                    if "base_url" in provider or "base_url" not in inherited:
                        upstream = provider_upstream(merged, native)
                        if not upstream:
                            raise ValueError("远程所选 provider 缺少可转发的上游地址。")
                        route(["model_providers", provider_id, "base_url"], upstream)
                except ValueError:
                    if provider_id in selected:
                        raise
                    continue
                change(["model_providers", provider_id, "supports_websockets"], False)
            if name == "config.toml" or "enable_request_compression" in document.get("features", {}):
                change(["features", "enable_request_compression"], False)
            for profile, value in document.get("profiles", {}).items():
                if "enable_request_compression" in value.get("features", {}):
                    change(["profiles", profile, "features", "enable_request_compression"], False)
        updated = tomlkit.dumps(document)
        original = (files[name] or b"").decode("utf-8-sig")
        if updated != original:
            bom = b"\xef\xbb\xbf" if (files[name] or b"").startswith(b"\xef\xbb\xbf") else b""
            edits[name] = bom + updated.encode("utf-8")
    return edits, routes, fields


def managed_fields(record, name):
    return record.get("fields", {}).get(name, []) + record.get("previous_fields", {}).get(name, [])


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
    def __init__(self, directory, endpoint, *, ssh=None, start=None, image_settings=None):
        self.directory = Path(directory)
        self.endpoint = endpoint
        self.ssh = ssh or SshControl()
        self.start = start or subprocess.Popen
        self.gateway = None
        self.processes = {}
        self.current_accounts = {}
        self.image_settings = image_settings
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
        with self.guard:
            if self.closed:
                return False
            for key, record in self.records():
                host_key = key.partition("-")[0]
                if (device is not None and device != host_key) or self.current_accounts.get(host_key, key) != key:
                    continue
                if (record.get("enabled") and self.retry_at.get(host_key, 0) <= now
                        and (key not in self.processes or self.processes[key].poll() is not None)):
                    return True
            return False

    def _save(self, key, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / (key + ".json"), record)

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
        images = self._images(host, key, record, files, account, False, action) if action == "restore" else None
        if action in {"activate", "restore"}:
            if action == "activate":
                if not record:
                    record = {"token": secrets.token_hex(24), "port": 49152 + secrets.randbelow(16384),
                              "proof": secrets.token_hex(24), **account}
                endpoint = f"http://127.0.0.1:{record['port']}/relay/{record['token']}"
            else:
                endpoint = ""
            if record:
                edits, routes, fields = config_changes(files, snapshot, record, endpoint)
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
                    try:
                        self._ensure(host, key, record)
                    except (OSError, ValueError):
                        self._save(key, prior)
                        raise
                if edits:
                    expected = {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                for name, raw in files.items()}
                    updates = {name: base64.b64encode(raw).decode("ascii") for name, raw in edits.items()}
                    outcome = self._call(host, "write", expected=expected, updates=updates, **account)
                    changed = bool(outcome["changed"])
                    files.update(edits)
                active = bool(endpoint)
                if endpoint:
                    record["reload_required"] = bool(changed or record.get("reload_required"))
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
        if images is not None:
            result["image_storage"] = images
        if action == "reconnect":
            reconnect = self._call(host, "reconnect", **account)
            result["codex_reconnect"] = reconnect
            result["codex_restart"] = reconnect["codex_restart"]
            if record and reconnect["state"] == "reloaded" and not reconnect.get("warning"):
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
