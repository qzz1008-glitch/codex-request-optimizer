"""Reversible connection of WSL Codex and read-only access to its SQLite indexes."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections import defaultdict
from urllib.parse import urlsplit

from .background_session_labels import BackgroundSessionLabels
from .config_link import ConfigLink, PROVIDER
from .connection_plan import ConfigEdit, ConfigSnapshot, ConfigurationBatch
from .linux_images import LinuxImageTarget, linux_image_edits, probe_image_directory
from .native_state import LABEL_READER
from .providers import ProviderManager
from .network_diagnostics import safe_text
from .remote_control import LINUX_IMAGE_SCRIPT
from .router import atomic_json
from .session_summary import external_labels


PROBE_SCRIPT = r'''
set -eu
exec 3<>/dev/tcp/127.0.0.1/"$1"
printf 'GET /api/wsl-probe HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$1" >&3
response=
IFS= read -r -t 3 -N 4096 response <&3 || true
printf '%s' "$response"
'''

LABEL_LAUNCH = 'cd "$1" && exec python3 -I -S -'
LABEL_QUERY_LAUNCH = LABEL_LAUNCH + ' "${CODEX_HOME:-$HOME/.codex}"'

LABEL_SCRIPT = LABEL_READER + r'''
import json,sys
from pathlib import Path
request=json.load(sys.stdin)
if Path(sys.argv[1]).resolve() == Path(request["home"]).resolve():
    result = read_label_data(request["home"], request["ids"])
else:
    result = {"error": "codex_home_changed"}
print("CPI_LABELS=" + json.dumps(result, ensure_ascii=True))
'''

IMAGE_SCRIPT = LINUX_IMAGE_SCRIPT + r'''
import json
expected = Path(REQUEST.pop("home")).resolve()
actual = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
try:
    if expected != actual or actual.stat().st_uid != os.getuid():
        raise ValueError("WSL 登录账号或 Codex 配置目录已变化，请重新接入。")
    response = linux_image_control(actual, REQUEST)
except ValueError as exc:
    response = {"error": str(exc)[:240]}
except OSError as exc:
    response = {"error": "无法读写 WSL 图片目录（" + type(exc).__name__ + "）。"}
print("CPI_IMAGES=" + json.dumps(response))
'''


class WslError(ValueError):
    def __init__(self, reason, *, exit_code=None, detail=""):
        super().__init__(reason)
        self.exit_code = exit_code
        self.detail = safe_text(detail, limit=1000)


def warning(distribution, error):
    value = {"distribution": distribution, "reason": str(error) if isinstance(error, WslError) else type(error).__name__}
    if isinstance(error, WslError):
        if error.exit_code is not None:
            value["exit_code"] = error.exit_code
        if error.detail:
            value["detail"] = error.detail
    return value


def index_labels(config, ids):
    """Plain JSONL can be read over WSL's file share without a Linux runtime."""
    found, wanted = {}, set(ids)
    try:
        path = Path(config).parent / "session_index.jsonl"
        offset = max(0, path.stat().st_size - 1024 * 1024)
        with path.open("rb") as stream:
            stream.seek(offset)
            lines = stream.read(1024 * 1024).split(b"\n")
        for line in reversed(lines[1:-1] if offset else lines[:-1]):
            try:
                item = json.loads(line)
                key, title = item.get("id"), item.get("thread_name")
                if key in wanted and key not in found and isinstance(title, str) and title.strip():
                    label = {"title": title[:512]}
                    try:
                        label["updated_at"] = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        pass
                    found[key] = label
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return found


def decode_output(value):
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16", errors="replace")
    return value.decode("utf-16-le" if b"\0" in value[:128] else "utf-8", errors="replace")


def validate_target(distribution, home):
    if (not isinstance(distribution, str) or not re.fullmatch(r"[\w .-]{1,128}", distribution)
            or distribution in {".", ".."}):
        raise WslError("invalid_distribution")
    if (not isinstance(home, str) or not home.startswith("/") or home.startswith("//")
            or ".." in home.split("/") or "\\" in home or any(ord(c) < 32 for c in home)
            or len(home) > 2048 or home == "/"):
        raise WslError("invalid_codex_home")
    return distribution, str(PurePosixPath(home))


class LocalWsl:
    def __init__(self):
        self.executable = shutil.which("wsl.exe") if os.name == "nt" else None

    def run(self, arguments, *, data=None, timeout=5):
        if not self.executable:
            raise WslError("wsl_unavailable")
        try:
            result = subprocess.run([self.executable, *arguments], input=data, capture_output=True,
                                    timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise WslError("wsl_timeout") from None
        except OSError:
            raise WslError("wsl_unavailable") from None
        if result.returncode:
            raise WslError("wsl_command_failed", exit_code=result.returncode,
                           detail=decode_output(result.stderr[-3000:]))
        if len(result.stdout) > 4 * 1024 * 1024:
            raise WslError("wsl_output_too_large")
        return decode_output(result.stdout)

    def running(self):
        if not self.executable:
            return []
        text = self.run(["--list", "--running", "--quiet"])
        names = [line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()]
        return [name for name in names if re.fullmatch(r"[\w .-]{1,128}", name)
                and not name.lower().startswith(("docker", "rancher", "podman"))][:8]

    def home(self, distribution):
        text = self.run(["-d", distribution, "--exec", "sh", "-lc",
                         'printf "\\nCPI_CODEX_HOME=%s\\n" "${CODEX_HOME:-$HOME/.codex}"'])
        homes = [line.removeprefix("CPI_CODEX_HOME=") for line in text.splitlines()
                 if line.startswith("CPI_CODEX_HOME=")]
        if len(homes) != 1:
            raise WslError("codex_home_not_found")
        return validate_target(distribution, homes[0])[1]

    def config(self, distribution, home):
        validate_target(distribution, home)
        for host in ("wsl.localhost", "wsl$"):
            path = Path("\\\\" + host + "\\" + distribution).joinpath(*PurePosixPath(home).parts[1:], "config.toml")
            if path.is_file():
                return path
        return None

    def probe(self, distribution, port, marker):
        try:
            text = self.run(["-d", distribution, "--exec", "bash", "-s", "--", str(port)],
                            data=PROBE_SCRIPT.encode("utf-8"))
        except WslError as exc:
            raise WslError("relay_unreachable", exit_code=exc.exit_code, detail=exc.detail or str(exc)) from None
        try:
            headers, body = text.replace("\r\n", "\n").split("\n\n", 1)
            if not re.match(r"HTTP/1\.[01] 200(?:\s|$)", headers):
                raise WslError("relay_unreachable", detail=headers.splitlines()[0][:200])
            data = json.loads(body)
        except WslError:
            raise
        except (ValueError, TypeError):
            raise WslError("relay_unreachable") from None
        if not isinstance(data, dict) or data.get("instance") != marker:
            raise WslError("relay_instance_mismatch")

    def labels(self, distribution, home, ids):
        # stdin avoids Windows/WSL quoting of a multiline Python -c argument.
        # WSL inherits the Windows launch directory; a broken /mnt drive must
        # never enter Python's imports through cwd, PYTHONPATH or site hooks.
        script = LABEL_SCRIPT.replace('request=json.load(sys.stdin)', 'request=' + repr({"home": home, "ids": ids}))
        text = self.run(["-d", distribution, "--exec", "sh", "-lc", LABEL_QUERY_LAUNCH,
                         "codex-inspector-labels", home], data=script.encode("utf-8"))
        lines = [line[len("CPI_LABELS="):] for line in text.splitlines() if line.startswith("CPI_LABELS=")]
        if len(lines) != 1:
            raise WslError("metadata_invalid_response")
        result = json.loads(lines[0])
        if not isinstance(result, dict):
            raise WslError("metadata_invalid_response")
        if result.get("error") == "codex_home_changed":
            raise WslError("codex_home_changed")
        return result

    def images(self, distribution, home, action, **options):
        validate_target(distribution, home)
        request = {"home": home, "action": action, **options}
        script = "REQUEST = " + repr(request) + "\n" + IMAGE_SCRIPT
        text = self.run(["-d", distribution, "--exec", "sh", "-lc", "cd / && exec python3 -I -S -"],
                        data=script.encode("utf-8"), timeout=30)
        lines = [line.removeprefix("CPI_IMAGES=") for line in text.splitlines() if line.startswith("CPI_IMAGES=")]
        if len(lines) != 1:
            raise WslError("image_operation_unconfirmed")
        result = json.loads(lines[0])
        if not isinstance(result, dict):
            raise WslError("image_operation_unconfirmed")
        if result.get("error"):
            raise ValueError(result["error"])
        return result

    def migrate_providers(self, distribution, home, aliases, *, require_reload=False):
        # Native Linux SQLite owns its WAL locks; never write it through UNC.
        request = {"home": home, "backup_dir": str(PurePosixPath(home) / "provider-migration-backups"),
                   "aliases": aliases, "target": PROVIDER, "require_reload": require_reload}
        script = Path(__file__).with_name("provider_migration_worker.py").read_text(encoding="utf-8")
        script = script.replace("request = json.load(sys.stdin)", "request = " + repr(request))
        text = self.run(["-d", distribution, "--exec", "sh", "-lc", LABEL_LAUNCH,
                         "codex-inspector-provider-migration", home], data=script.encode("utf-8"), timeout=15)
        lines = [line.removeprefix("CPI_PROVIDER_MIGRATION=") for line in text.splitlines()
                 if line.startswith("CPI_PROVIDER_MIGRATION=")]
        if len(lines) != 1:
            raise WslError("metadata_invalid_response")
        result = json.loads(lines[0])
        if not isinstance(result, dict) or not {"changed", "backups", "warnings"} <= result.keys():
            raise WslError("metadata_invalid_response")
        return result


class WslConnection:
    def __init__(self, data_dir, backend=None, *, label_updates=None, image_settings=None):
        self.directory = Path(data_dir) / "wsl-connections"
        self.backend = backend or LocalWsl()
        self.result = {"targets": [], "warnings": [], "changed": False, "reload_required": False}
        self._lock = threading.RLock()
        self._running_at, self._running = 0, []
        self.labels = BackgroundSessionLabels(self._find_labels, updates=label_updates)
        self.metadata_error = ""
        self._metadata_retry = 0
        self._discovered_at = 0
        self._discovered_running = None
        self._label_targets = []
        self._metadata_errors = {}
        self.image_settings = image_settings
        self.image_targets = {}

    def image_target(self, key):
        return self.image_targets.get(key)

    def _images(self, directory, target, link, active):
        if self.image_settings is None:
            return {}, False
        key = directory.name
        mode, data_dir = self.image_settings("wsl:" + key) if active else ("native", "")
        previous_path = directory / "image-tool.json"

        def send(operation, **options):
            return self.backend.images(target["distribution"], target["home"], operation, **options)

        status = {"data_dir": data_dir, "directory": "", "error": ""}
        if active:
            try:
                status["directory"] = probe_image_directory(send, data_dir)
            except (OSError, ValueError) as exc:
                status["error"] = safe_text(getattr(exc, "detail", "") or str(exc), limit=240)
        changed = False
        ready = None
        try:
            if mode != "optimized" or status["directory"]:
                saved = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
                previous = [*saved.get("pending", []), *saved.get("fields", [])]
                raw = ConfigSnapshot.read(link.config)
                edit, fields, uninstall, changed = linux_image_edits(raw.content, previous, mode, send)
                if edit is not None:
                    atomic_json(previous_path, {"fields": previous, "pending": fields})
                    ConfigurationBatch((raw,), (ConfigEdit(raw, edit.decode("utf-8-sig")),)).apply(link)
                    changed = True
                if changed or fields != previous or "pending" in saved:
                    atomic_json(previous_path, {"fields": fields})
                if mode == "optimized":
                    ready = LinuxImageTarget(status["directory"], send)
                elif uninstall:
                    send("image_uninstall")
        except (OSError, ValueError) as exc:
            status["error"] = safe_text(getattr(exc, "detail", "") or str(exc), limit=240)
        if ready:
            self.image_targets[key] = ready
        else:
            self.image_targets.pop(key, None)
        return status, changed

    def refresh_images(self, target_ids=None):
        """Apply image settings to the current connection without relinking providers."""
        with self._lock:
            changed = []
            selections = {item.get("id"): item for item in self.result["targets"] if item.get("active")}
            for directory, target in self._targets():
                identity = "wsl:" + directory.name
                if identity not in selections or target_ids is not None and identity not in target_ids:
                    continue
                current = selections[identity]
                storage, updated = self._images(directory, target,
                    ConfigLink(Path(target["config"]), directory / "link.json"), True)
                current["image_storage"] = storage
                if updated:
                    changed.append({**current, "changed": True})
            return changed

    def _distributions(self, *, refresh=False):
        if refresh or time.monotonic() - self._running_at > 15:
            self._running = self.backend.running()
            self._running_at = time.monotonic()
        return self._running

    def _targets(self):
        for path in sorted(self.directory.glob("*/target.json")):
            try:
                value = json.loads(path.read_text(encoding="utf8"))
                validate_target(value["distribution"], value["home"])
                yield path.parent, value
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _discover(self, running):
        """Remember read-only locations independently of successful routing."""
        detected, warnings = [], []
        self._discovered_at = time.monotonic()
        for distribution in running:
            try:
                home = self.backend.home(distribution)
                config = self.backend.config(distribution, home)
                if config is None:
                    warnings.append({"distribution": distribution, "home": home, "reason": "config_not_found"})
                    continue
                identity = hashlib.sha256((distribution + "\n" + home).encode()).hexdigest()[:24]
                directory = self.directory / identity
                target = {"distribution": distribution, "home": home, "config": str(config)}
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "target.json"
                text = json.dumps(target)
                if not path.exists() or path.read_text(encoding="utf8") != text:
                    staging = directory / "target.new"
                    staging.write_text(text, encoding="utf8")
                    staging.replace(path)
                detected.append((directory, target, config))
            except (WslError, OSError) as exc:
                warnings.append(warning(distribution, exc))
        self._label_targets = detected
        self._discovered_running = tuple(running)
        return detected, warnings

    @staticmethod
    def _uses_our_routes(config, endpoint, routes):
        data = tomllib.loads(config.read_text(encoding="utf-8-sig"))
        urls = [data.get("openai_base_url")]
        urls += [provider.get("base_url") for provider in data.get("model_providers", {}).values()
                 if isinstance(provider, dict)]
        own = {endpoint, *(endpoint + "/p/" + key for key in routes)}
        return any(isinstance(url, str) and url in own for url in urls)

    def _metadata_status(self):
        errors = list(self._metadata_errors.values())[:8]
        self.metadata_error = errors[0]["reason"] if errors else ""
        path = self.directory / "metadata-status.json"
        text = json.dumps({"errors": errors}, ensure_ascii=False)
        if path.exists() and path.read_text(encoding="utf8") == text:
            return
        if errors or path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            staging = path.with_suffix(".new")
            staging.write_text(text, encoding="utf8")
            staging.replace(path)

    def apply(self, connected, endpoint, upstream, routes, publish, *, retry_limit=None):
        with self._lock:
            routes = dict(routes)
            result = {"targets": [], "warnings": [], "changed": False, "reload_required": False}
            self.result = result
            try:
                running = self._distributions(refresh=True)
            except WslError as exc:
                # WSL's Windows stub is present even when WSL is not installed.
                if list(self._targets()):
                    result["warnings"].append(warning("WSL", exc))
                return result
            detected, warnings = self._discover(running)
            result["detected"] = [{**target, "id": "wsl:" + directory.name} for directory, target, _ in detected]
            result["warnings"].extend(warnings)
            if connected:
                candidates = detected
                result["deferred"] = [{**target, "id": "wsl:" + directory.name}
                                      for directory, target in self._targets() if target["distribution"] not in running][:16]
            else:
                candidates = []
                for directory, target in self._targets():
                    if target["distribution"] not in running:
                        if (directory / "link.json").is_file():
                            result["warnings"].append({"distribution": target["distribution"], "reason": "restore_pending"})
                        continue
                    candidates.append((directory, target, Path(target["config"])))
            for directory, target, config in candidates:
                try:
                    link = ConfigLink(config, directory / "link.json")
                    manager = ProviderManager(link, endpoint, upstream, wsl_connection_key=directory.name)
                    def publish_routes(updated):
                        nonlocal routes
                        publish(updated)
                        routes = updated
                    if connected:
                        own = urlsplit(endpoint)
                        marker = hashlib.sha256(endpoint.encode()).hexdigest()
                        # Verify this exact Windows instance before any Linux write.
                        self.backend.probe(target["distribution"], own.port, marker)
                        update = manager.sync(routes, publish_routes, retry_limit=retry_limit,
                            session_migrator=lambda aliases, **guard: self.backend.migrate_providers(
                                target["distribution"], target["home"], aliases, **guard))
                        storage, image_changed = self._images(directory, target, link, True)
                    else:
                        if not config.is_file() or any(not Path(name).is_file() for name in link._state().get("files", {})):
                            raise WslError("restore_pending")
                        if not link._state():
                            if not self._uses_our_routes(config, endpoint, routes):
                                continue
                            # Manually connected URLs still identify their saved
                            # upstream. Record/backup them before native restore.
                            manager.sync(routes, publish_routes,
                                session_migrator=lambda aliases, **guard: self.backend.migrate_providers(
                                    target["distribution"], target["home"], aliases, **guard))
                        storage, image_changed = self._images(directory, target, link, False)
                        update = link.restore()
                    changed = bool(update.get("changed", update.get("reload_required")) or image_changed)
                    reload_required = bool(update.get("reload_required") or image_changed)
                    migration = update.get("session_migration", {})
                    result["targets"].append({**target, "id": "wsl:" + directory.name, "active": connected,
                                               "changed": changed, "reload_required": reload_required,
                                               "image_storage": storage,
                                               "migrated_sessions": migration.get("changed", 0),
                                               "migration_waiting": bool(migration.get("waiting_for_reload"))})
                    result["changed"] |= changed
                    result["reload_required"] |= reload_required
                    result["warnings"].extend({"distribution": target["distribution"],
                                               **{key: item[key] for key in ("reason", "file", "detail") if key in item}}
                                              for item in update.get("warnings", []))
                except (OSError, ValueError) as exc:
                    result["warnings"].append(warning(target["distribution"], exc))
            self.labels.clear()
            if not connected:
                self.image_targets.clear()
            return result

    def reconcile_sessions(self, endpoint, upstream, routes):
        """Check known installations as they become available without starting them."""
        with self._lock:
            result = {"targets": [], "warnings": [], "deferred": [], "changed": False, "reload_required": False}
            self.result = result
            targets = list(self._targets())
            if not targets:
                return result
            try:
                running = self._distributions(refresh=True)
            except WslError as exc:
                result["warnings"].append(warning("WSL", exc))
                return result
            for directory, target in targets:
                if target["distribution"] not in running:
                    result["deferred"].append({**target, "id": "wsl:" + directory.name})
                    continue
                try:
                    link = ConfigLink(Path(target["config"]), directory / "link.json")
                    update = ProviderManager(link, endpoint, upstream).reconcile_sessions(routes,
                        session_migrator=lambda aliases, **guard: self.backend.migrate_providers(
                            target["distribution"], target["home"], aliases, **guard))
                    changed = bool(update["changed"])
                    reload_required = bool(update.get("waiting_for_reload")
                                           or changed and not update.get("verified_after_reload"))
                    result["targets"].append({**target, "active": False, "changed": changed,
                                              "reload_required": reload_required,
                                              "migrated_sessions": update["changed"],
                                              "migration_waiting": bool(update.get("waiting_for_reload"))})
                    result["changed"] |= changed
                    result["reload_required"] |= reload_required
                    result["warnings"].extend({"distribution": target["distribution"], **item}
                                              for item in update["warnings"])
                except (OSError, ValueError) as exc:
                    result["warnings"].append(warning(target["distribution"], exc))
            return result

    def _find_labels(self, requests):
        matches = defaultdict(list)
        with self._lock:
            if time.monotonic() < self._metadata_retry:
                return matches
            try:
                running = self._distributions(refresh=True)
                if tuple(running) != self._discovered_running or time.monotonic() - self._discovered_at > 60:
                    self._discover(running)
                targets = self._label_targets
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.metadata_error = str(exc) if isinstance(exc, WslError) else type(exc).__name__
                self._metadata_retry = time.monotonic() + 15
                return matches

        errors = {}
        for directory, target, config in targets:
            batch = {request[0]: request for request in requests if request[1] in (None, directory.name)}
            if not batch:
                continue
            labels, error = self._read_target_labels(target, config, sorted(batch))
            errors[directory.name] = error
            for thread, label in labels.items():
                matches[batch[thread]].append({**label, "wsl_connection_key": directory.name})

        with self._lock:
            active = {directory.name for directory, _, _ in self._label_targets}
            self._metadata_errors.update(errors)
            self._metadata_errors = {key: error for key, error in self._metadata_errors.items() if error and key in active}
            if any(error and error["reason"] == "codex_home_changed" for error in errors.values()):
                self._discovered_running = None
            try:
                self._metadata_status()
            except (OSError, ValueError) as exc:
                self.metadata_error = type(exc).__name__
        return matches

    def _read_target_labels(self, target, config, ids):
        distribution, home = target["distribution"], target["home"]
        try:
            raw = self.backend.labels(distribution, home, ids)
            return external_labels(raw, ids, index_labels(config, ids)), None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            error = warning(distribution, exc)
        if error["reason"] != "codex_home_changed":
            try:
                if self.backend.home(distribution) == home:
                    return index_labels(config, ids), error
                error = warning(distribution, WslError("codex_home_changed"))
            except (OSError, WslError):
                pass
        return {}, error
