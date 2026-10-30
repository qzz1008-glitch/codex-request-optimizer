"""Discover standard Codex providers and publish reversible local HTTP routes."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import secrets
import tomllib
from urllib.parse import urlsplit

import tomlkit

from .config_link import ConfigLink, PROVIDER, validate_upstream
from .connection_policy import native_compression, standardize
from .sharing_config import SHARED_PROVIDER

MODE = "all-providers-v1"


def config_paths(config):
    return [config, *(path for path in sorted(config.parent.glob("*.config.toml"))
                      if path.resolve() != config.resolve())]


def _get(document, keys):
    value = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _put(document, keys, value, present=True):
    target = document
    for key in keys[:-1]:
        if key not in target:
            target[key] = tomlkit.table()
        target = target[key]
    if present:
        target[keys[-1]] = value
    else:
        target.pop(keys[-1], None)


def _write_plans(link, plans):
    written, backups = [], []
    try:
        for path, original, updated in plans:
            current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            if current != original:
                raise ValueError("Codex 配置在扫描期间发生变化，请重新检测")
            before = path.read_bytes() if path.exists() else None
            written.append((path, updated, before))
            backups.append(ConfigLink(path, link.state_path)._write(updated))
    except Exception:
        for path, updated, before in reversed(written):
            # Never overwrite a concurrent edit, even during rollback.
            if not path.exists() or path.read_text(encoding="utf-8-sig") != updated:
                continue
            if before is None:
                path.unlink()
            else:
                staging = path.with_name(path.name + ".inspector-rollback")
                staging.write_bytes(before)
                staging.replace(path)
        raise
    return backups


class ProviderManager:
    def __init__(self, link, endpoint, default_upstream):
        self.link = link
        self.endpoint = endpoint.rstrip("/")
        self.default_upstream = default_upstream

    def sync(self, saved_routes, publish, *, retry_limit=None):
        """Publish routes/recovery first, then atomically update each TOML file.

        Failed batches roll back their own writes. Concurrent edits are kept,
        with recovery information and extra routes available for a safe retry.
        """
        prior = self.link._state()
        state = deepcopy(prior) if prior.get("mode") == MODE else {
            "mode": MODE, "files": {}, "legacy": prior}
        state["connection_policy"] = 1
        routes = deepcopy(saved_routes)
        paths = config_paths(self.link.config)
        plans, warnings = [], []
        detected = set()
        base = tomllib.loads(self.link.config.read_text(encoding="utf-8-sig")) if self.link.config.exists() else {}
        for path in paths:
            text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            data = tomllib.loads(text)
            document = tomlkit.parse(text)
            standardize(document, data, base,
                        lambda value, provider: self._original(value, provider, routes),
                        self._native_upstream(), primary=path == self.link.config)
            native_compression(document, False)
            data = tomllib.loads(tomlkit.dumps(document))
            records = state["files"].setdefault(str(path.resolve()), [])
            fields = []
            providers = data.get("model_providers", {})
            for provider_id, provider in providers.items():
                if provider_id == SHARED_PROVIDER:
                    continue
                if not isinstance(provider, dict):
                    continue
                inherited = base.get("model_providers", {}).get(provider_id, {}) if path != self.link.config else {}
                if provider.get("wire_api", inherited.get("wire_api", "responses")) != "responses":
                    warnings.append({"file": path.name, "provider": provider_id, "reason": "unsupported_wire_api"})
                    continue
                if "base_url" in provider:
                    fields.append((["model_providers", provider_id, "base_url"], provider_id, provider["base_url"]))
                elif "base_url" in inherited and provider.get("supports_websockets"):
                    self._record(records, data, ["model_providers", provider_id, "supports_websockets"], False)
                    _put(document, ["model_providers", provider_id, "supports_websockets"], False)
                elif not inherited:
                    warnings.append({"file": path.name, "provider": provider_id, "reason": "missing_http_base_url"})
            # Built-in OpenAI uses a root override; its reserved ID cannot be redefined.
            if path == self.link.config or "openai_base_url" in data:
                original = data.get("openai_base_url")
                native_record = next((r for r in records if r["keys"] == ["openai_base_url"]), None)
                if native_record and not native_record["present"] and original == native_record["applied"]:
                    original = self._native_upstream()
                if not original:
                    original = self._native_upstream()
                fields.append((["openai_base_url"], "openai", original))
            for keys, provider_id, current in fields:
                detected.add(provider_id)
                upstream = self._original(current, provider_id, routes)
                upstream = validate_upstream(upstream)
                target = urlsplit(upstream)
                own = urlsplit(self.endpoint)
                if target.hostname in {"localhost", "127.0.0.1", "::1"} and target.port == own.port:
                    raise ValueError("provider 上游不能指向采集器自身")
                identity = str(path.resolve()) + "\n" + provider_id
                route_id = next((key for key, value in routes.items()
                                 if value.get("identity") == identity and value.get("upstream") == upstream), None)
                if route_id is None:
                    route_id = secrets.token_hex(12)
                routes[route_id] = {"identity": identity, "provider": provider_id, "upstream": upstream}
                applied = self.endpoint + "/p/" + route_id
                self._record(records, data, keys, applied, original=upstream if upstream != current else None)
                _put(document, keys, applied)
                if len(keys) > 1:
                    ws_keys = keys[:-1] + ["supports_websockets"]
                    self._record(records, data, ws_keys, False)
                    _put(document, ws_keys, False)
            for provider_id, provider in data.get("model_providers", {}).items():
                if provider_id == SHARED_PROVIDER:
                    continue
                if not isinstance(provider, dict) or provider.get("wire_api", "responses") != "responses":
                    continue
                for key, value in (("stream_max_retries", retry_limit), ("request_max_retries", 0)):
                    retry_keys = ["model_providers", provider_id, key]
                    if retry_limit is not None:
                        self._record(records, data, retry_keys, value)
                        _put(document, retry_keys, value)
                    else:
                        record = next((r for r in records if r["keys"] == retry_keys), None)
                        if record is not None:
                            present, current = _get(data, retry_keys)
                            if present and current == record["applied"]:
                                _put(document, retry_keys, record["before"], record["present"])
                            records.remove(record)
            updated = tomlkit.dumps(document)
            tomllib.loads(updated)
            if updated != text:
                plans.append((path, text, updated))
        # Settings contain no credentials; headers and auth remain in Codex's TOML.
        publish(routes)
        self.link._write_state(state)
        try:
            backups = _write_plans(self.link, plans)
        except Exception:
            if all((path.read_text(encoding="utf-8-sig") if path.exists() else "") == original
                   for path, original, _ in plans):
                self.link._write_state(prior)
            raise
        return {"active": True, "changed": bool(plans), "reload_required": bool(plans),
                "provider_count": len(detected),
                "config_files": len(paths), "backups": backups, "warnings": warnings}

    def _original(self, current, provider_id, routes):
        if not isinstance(current, str):
            raise ValueError("provider 上游地址必须是字符串")
        parsed = urlsplit(current)
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and "/p/" in parsed.path:
            route = routes.get(parsed.path.rsplit("/p/", 1)[1])
            if route:
                return route["upstream"]
        if (current == self.endpoint or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.path.rstrip("/") == urlsplit(self.endpoint).path.rstrip("/")):
            legacy = self.link._state()
            if legacy.get("mode") == MODE:
                legacy = legacy.get("legacy", {})
            if provider_id == PROVIDER:
                return legacy.get("upstream") or self.default_upstream
        if current.startswith(self.endpoint + "/p/"):
            route = routes.get(current[len(self.endpoint + "/p/"):])
            if route:
                return route["upstream"]
            raise ValueError("provider 路由缺少原上游记录，请从备份恢复")
        return current

    def _native_upstream(self):
        # Match the existing login mode without reading or copying credentials.
        import json
        auth_file = self.link.config.parent / "auth.json"
        try:
            mode = json.loads(auth_file.read_text(encoding="utf8")).get("auth_mode", "chatgpt")
        except (OSError, ValueError):
            mode = "chatgpt"
        return "https://api.openai.com/v1" if mode == "apikey" else "https://chatgpt.com/backend-api/codex"

    @staticmethod
    def _record(records, data, keys, applied, original=None):
        present, before = _get(data, keys)
        record = next((value for value in records if value["keys"] == keys), None)
        if record is None:
            if original is not None:
                before = original
            records.append({"keys": keys, "present": present, "before": before, "applied": applied})
        else:
            if before != record["applied"] or not present:
                record.update(present=present, before=before)
            record["applied"] = applied


def restore_providers(link, state):
    plans = []
    for filename, records in state["files"].items():
        path = Path(filename)
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(original)
        document = tomlkit.parse(original)
        for record in records:
            present, value = _get(data, record["keys"])
            # Preserve later explicit user changes to any individual field.
            if present and value == record["applied"]:
                _put(document, record["keys"], record["before"], record["present"])
        if state.get("connection_policy"):
            native_compression(document, True)
        updated = tomlkit.dumps(document)
        if updated != original:
            plans.append((path, original, updated))
    backups = _write_plans(link, plans)
    legacy = state.get("legacy", {})
    if legacy and not state.get("connection_policy"):
        link._write_state(legacy)
        text, _ = link._read()
        from .config_link import BEGIN
        if BEGIN in text:
            result = link.restore()
            backups.append(result.get("backup", ""))
    link._write_state({})
    return {"active": False, "backups": backups, "reload_required": bool(backups)}
