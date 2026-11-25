"""Provider identity checks and migration of saved native configuration."""
import os
import sys
import tomllib

from .config_link import PROVIDER
from .connection_policy import codex_auth, provider_identity
from .configuration_layers import merge_layers
from .desktop_restart import windows_writer_before
from .provider_migration_worker import linux_writer_before, migrate


def compatible_aliases(data, original_upstream, native_upstream):
    providers = data.get("model_providers", {})
    canonical = providers.get(PROVIDER, {})
    if not codex_auth(canonical):
        return set()
    identity = provider_identity(canonical)
    upstream = original_upstream(canonical.get("base_url", native_upstream), PROVIDER)
    candidates = dict(providers)
    if native_upstream.endswith("/backend-api/codex"):
        candidates["openai"] = {"wire_api": "responses", "requires_openai_auth": True,
                                "base_url": data.get("openai_base_url", native_upstream)}
    return {key for key, value in candidates.items() if key != PROVIDER and isinstance(value, dict)
            and codex_auth(value) and provider_identity(value) == identity
            and original_upstream(value.get("base_url", native_upstream), key) == upstream}


def provider_selection_changed(before, after):
    def selections(value):
        data = tomllib.loads(value)
        default = data.get("model_provider", "openai")
        profiles = {key: profile["model_provider"] for key, profile in data.get("profiles", {}).items()
                    if isinstance(profile, dict) and "model_provider" in profile}
        return default, profiles

    return selections(before) != selections(after)


def local_writer_before(home, revision):
    if os.name == "nt":
        return windows_writer_before(home, revision)
    if sys.platform.startswith("linux"):
        return linux_writer_before(home, revision)
    raise OSError("Codex process lifetime is unavailable")


def migrate_sessions(link, aliases, migrator=None, *, require_reload=False):
    if not aliases:
        return {"changed": 0, "databases": 0, "backups": [], "warnings": []}
    try:
        return (migrator(aliases, require_reload=require_reload) if migrator else
                migrate(link.config.parent, link.state_path.parent / "provider-migration-backups", aliases, PROVIDER,
                        writer_before=local_writer_before, require_reload=require_reload, config_path=link.config))
    except (OSError, ValueError) as exc:
        # A Linux worker failure must retain the TOML change and its restart
        # feedback. Report the pending metadata repair for the existing retry.
        detail = type(exc).__name__ + ": " + str(exc)
        if getattr(exc, "detail", ""):
            detail += ": " + exc.detail
        return {"changed": 0, "databases": 0, "backups": [], "warnings": [
            {"file": link.config.name, "reason": "session_provider_migration_failed", "detail": detail[:400]}]}


def migration_aliases(documents, original_upstream, native_upstream):
    """Reject conflicting declarations without conflating independent profiles."""
    _, primary, excluded = documents[0]
    if primary.get("model_provider") != PROVIDER:
        return set(), []
    aliases = compatible_aliases(primary, original_upstream, native_upstream) - excluded
    warnings = []
    def inspect(scope, layer, parent, excluded):
        effective = merge_layers(parent, layer)
        compatible = compatible_aliases(effective, original_upstream, native_upstream) - excluded
        # Only declarations that redefine an old ID can conflict with its main
        # connection. An independent profile can keep its own canonical route.
        overridden = set(layer.get("model_providers", {}))
        if "openai_base_url" in layer:
            overridden.add("openai")
        if layer.get("model_provider") != PROVIDER:
            overridden.add(layer.get("model_provider"))
        conflicts = (aliases & overridden) - compatible
        warnings.extend({"file": scope, "provider": provider, "reason": "session_provider_ambiguous"}
                        for provider in sorted(conflicts))
        aliases.difference_update(conflicts)
        return effective

    for index, (path, layer, excluded) in enumerate(documents):
        effective = primary if index == 0 else inspect(path.name, layer, primary, excluded)
        for name, profile in layer.get("profiles", {}).items():
            if isinstance(profile, dict):
                inspect(path.name + " · " + name, profile, effective, excluded)
    return aliases, warnings
