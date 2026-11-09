"""Discover standard Codex providers and publish reversible local HTTP routes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import tomllib
from urllib.parse import urlsplit

import tomlkit

from .config_link import PROVIDER, validate_upstream
from .connection_policy import native_compression, standardize
from .connection_plan import CONCURRENT_CHANGE, ConfigEdit, ConfigSnapshot, ConfigurationBatch
from .configuration_layers import merge_layers
from .provider_defaults import native_upstream, provider_upstream
from .provider_migration import migration_aliases, migrate_sessions
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


@dataclass(frozen=True, repr=False)
class ProviderPlan:
    batch: ConfigurationBatch
    recovery: ConfigSnapshot
    prior_state: dict
    state: dict
    routes: dict
    aliases: frozenset
    warnings: tuple
    provider_count: int
    paths: tuple

    def check(self):
        self.recovery.check()
        self.batch.check()


class ProviderManager:
    def __init__(self, link, endpoint, default_upstream):
        self.link = link
        self.endpoint = endpoint.rstrip("/")
        self.default_upstream = default_upstream

    def sync(self, saved_routes, publish, *, retry_limit=None, session_migrator=None):
        """Publish routes/recovery first, then atomically update each TOML file.

        Failed batches roll back their own writes. Concurrent edits are kept,
        with recovery information and extra routes available for a safe retry.
        """
        plan = self.plan(saved_routes, retry_limit=retry_limit)
        plan.check()
        # Publish before switching any URL so an in-flight client always finds
        # its route. Extra unpublished-in-config routes are harmless on failure.
        publish(plan.routes)
        plan.check()
        self.link._write_state(plan.state)
        try:
            backups = plan.batch.apply(self.link)
        except Exception:
            if all(ConfigSnapshot.read(source.path).content == source.content for source in plan.batch.sources):
                self.link._write_state(plan.prior_state)
            raise
        migration = migrate_sessions(self.link, sorted(plan.aliases), session_migrator)
        changed = bool(plan.batch.edits or migration["changed"])
        return {"active": True, "changed": changed, "reload_required": changed,
                "provider_count": plan.provider_count,
                "config_files": len(plan.paths), "backups": backups + migration["backups"],
                "warnings": [*plan.warnings, *migration["warnings"]],
                "session_migration": migration,
                "config_files_verified": True}

    def plan(self, saved_routes, *, retry_limit=None):
        """Resolve the complete known configuration batch without side effects.

        Connect, automatic resync and upgrade use this same plan. WSL supplies
        its own config root and metadata worker to the same implementation.
        """
        recovery = ConfigSnapshot.read(self.link.state_path)
        prior = json.loads(recovery.text) if recovery.content is not None else {}
        state = deepcopy(prior) if prior.get("mode") == MODE else {
            "mode": MODE, "files": {}, "legacy": prior}
        state["connection_policy"] = 1
        routes = deepcopy(saved_routes)
        paths = tuple(config_paths(self.link.config))
        sources = tuple(ConfigSnapshot.read(path) for path in paths)
        native = self._native_upstream()
        plans, warnings, prepared = [], [], []
        detected = set()
        base = tomllib.loads(sources[0].text)
        for source in sources:
            path, text = source.path, source.text
            data = tomllib.loads(text)
            document = tomlkit.parse(text)
            parent = base if path == self.link.config else prepared[0][2]
            excluded, skipped = self._unsupported_providers(data, parent, path, routes)
            warnings.extend(skipped)
            standardize(document, data, parent,
                        lambda value, provider: self._original(value, provider, routes),
                        native, primary=path == self.link.config, excluded=excluded)
            native_compression(document, False)
            data = tomllib.loads(tomlkit.dumps(document))
            records = state["files"].setdefault(str(path.resolve()), [])
            fields = []
            providers = data.get("model_providers", {})
            for provider_id, provider in providers.items():
                if provider_id == SHARED_PROVIDER or provider_id in excluded:
                    continue
                if not isinstance(provider, dict):
                    continue
                inherited = parent.get("model_providers", {}).get(provider_id, {}) if path != self.link.config else {}
                if provider.get("wire_api", inherited.get("wire_api", "responses")) != "responses":
                    warnings.append({"file": path.name, "provider": provider_id, "reason": "unsupported_wire_api"})
                    continue
                if "base_url" in provider:
                    fields.append((["model_providers", provider_id, "base_url"], provider_id, provider["base_url"]))
                elif "base_url" in inherited:
                    if provider.get("supports_websockets"):
                        self._record(records, data, ["model_providers", provider_id, "supports_websockets"], False)
                        _put(document, ["model_providers", provider_id, "supports_websockets"], False)
                elif (implicit := provider_upstream(merge_layers(inherited, provider), native)) is not None:
                    # Custom providers use their authentication's native
                    # default, independently of the built-in root override.
                    fields.append((["model_providers", provider_id, "base_url"], provider_id, implicit))
                elif not inherited:
                    warnings.append({"file": path.name, "provider": provider_id, "reason": "missing_http_base_url"})
            # Built-in OpenAI uses a root override; its reserved ID cannot be redefined.
            if "openai" not in excluded and (path == self.link.config or "openai_base_url" in data):
                original = data.get("openai_base_url")
                native_record = next((r for r in records if r["keys"] == ["openai_base_url"]), None)
                if native_record and not native_record["present"] and original == native_record["applied"]:
                    original = native
                if not original:
                    original = native
                fields.append((["openai_base_url"], "openai", original))
            for keys, provider_id, current in fields:
                detected.add(provider_id)
                try:
                    upstream = validate_upstream(self._original(current, provider_id, routes))
                except ValueError as exc:
                    raise ValueError(f"{path.name} · {'.'.join(keys)}: {exc}") from None
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
                if provider_id == SHARED_PROVIDER or provider_id in excluded:
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
            prepared.append((source, document, data, excluded))
        # Only equivalent OpenAI-account routes may share the stable ID. A
        # conflicting profile definition cannot authorize a global migration.
        original = lambda value, provider: self._original(value, provider, routes)
        aliases, conflicts = migration_aliases([(source.path, data, excluded) for source, _, data, excluded in prepared],
                                              original, native)
        warnings.extend(conflicts)
        selected = base.get("model_provider", "openai")
        if prepared[0][2].get("model_provider") == PROVIDER and selected != PROVIDER and selected not in aliases:
            raise ValueError("所选 provider 在不同配置中存在连接冲突，未切换默认提供商或迁移已有会话。")
        for source, document, _, _ in prepared:
            for profile in document.get("profiles", {}).values():
                if isinstance(profile, dict) and profile.get("model_provider") in aliases:
                    profile["model_provider"] = PROVIDER
            updated = tomlkit.dumps(document)
            tomllib.loads(updated)
            if updated != source.text:
                plans.append(ConfigEdit(source, updated))
        guard = lambda: self._check_inputs(paths, native)
        return ProviderPlan(ConfigurationBatch(sources, tuple(plans), (guard,)), recovery, prior, state, routes,
                            frozenset(aliases), tuple(warnings), len(detected), paths)

    def reconcile_sessions(self, saved_routes, *, session_migrator=None):
        """Upgrade disconnected installations without changing their TOML or routes."""
        documents = [(path, tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}, set())
                     for path in config_paths(self.link.config)]
        aliases, warnings = migration_aliases(documents,
            lambda value, provider: self._original(value, provider, saved_routes), self._native_upstream())
        result = migrate_sessions(self.link, sorted(aliases), session_migrator)
        result["warnings"] = warnings + result["warnings"]
        return result

    def _unsupported_providers(self, data, base, path, routes):
        """Optional endpoints must not veto a valid default connection.

        Missing local route recovery remains an integration error. Only address
        validation failures of an unselected endpoint may be excluded.
        """
        selected = data.get("model_provider", base.get("model_provider", "openai"))
        if selected not in {"openai", SHARED_PROVIDER}:
            selected_provider = merge_layers(base.get("model_providers", {}).get(selected, {}),
                                             data.get("model_providers", {}).get(selected, {}))
            if not selected_provider:
                raise ValueError(f"{path.name} · model_provider: 未找到所选 provider 的配置")
            if selected_provider.get("wire_api", "responses") != "responses":
                raise ValueError(f"{path.name} · model_provider: 所选 provider 不使用 Responses HTTP 接口")
            if provider_upstream(selected_provider, self._native_upstream()) is None:
                raise ValueError(f"{path.name} · model_provider: 无法确定所选 provider 的 HTTP 上游地址")
        excluded, warnings = set(), []
        providers = dict(data.get("model_providers", {}))
        if path != self.link.config and selected in base.get("model_providers", {}):
            providers.setdefault(selected, {})

        def invalid_reason(value):
            try:
                validate_upstream(value)
            except ValueError as exc:
                return str(exc)
            return None

        candidates = []
        for provider_id, provider in providers.items():
            if provider_id == SHARED_PROVIDER or not isinstance(provider, dict):
                continue
            inherited = base.get("model_providers", {}).get(provider_id, {}) if path != self.link.config else {}
            effective = merge_layers(inherited, provider)
            if effective.get("wire_api", "responses") != "responses" or "base_url" not in effective:
                continue
            candidates.append((f"model_providers.{provider_id}.base_url", provider_id, effective["base_url"]))
        if "openai_base_url" in data:
            candidates.append(("openai_base_url", "openai", data["openai_base_url"]))
        for field, provider_id, current in candidates:
            reason = invalid_reason(current)
            if reason is None:
                upstream = self._original(current, provider_id, routes)
                reason = invalid_reason(upstream)
            if reason is not None:
                if provider_id == selected:
                    raise ValueError(f"{path.name} · {field}: {reason}")
                excluded.add(provider_id)
                warnings.append({"file": path.name, "provider": provider_id,
                                 "reason": "unsupported_upstream", "detail": reason})
        return excluded, warnings

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
        return native_upstream(self.link.config)

    def _check_inputs(self, paths, native):
        # Token renewal does not invalidate a plan. Only a login-mode change
        # that changes the implicit HTTP destination does.
        if tuple(config_paths(self.link.config)) != paths or self._native_upstream() != native:
            raise ValueError(CONCURRENT_CHANGE)

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
    plans, sources = [], []
    for filename, records in state["files"].items():
        path = Path(filename)
        if not path.exists():
            continue
        source = ConfigSnapshot.read(path)
        sources.append(source)
        original = source.text
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
            plans.append(ConfigEdit(source, updated))
    backups = ConfigurationBatch(tuple(sources), tuple(plans)).apply(link)
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
