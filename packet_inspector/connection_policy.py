"""Standard Codex configuration policy; never changes native chat records."""
from copy import deepcopy

from .config_link import PROVIDER


def native_compression(document, enabled):
    document.setdefault("features", {})["enable_request_compression"] = enabled
    for profile in document.get("profiles", {}).values():
        if isinstance(profile, dict) and "enable_request_compression" in profile.get("features", {}):
            profile["features"]["enable_request_compression"] = enabled


def codex_auth(provider):
    return (provider.get("requires_openai_auth") is True
            and provider.get("wire_api", "responses") == "responses"
            and not any(provider.get(key) for key in ("env_key", "experimental_bearer_token", "auth", "aws")))


def standardize(document, data, base, original_upstream, native_upstream, *, primary=False):
    """Keep old IDs as aliases and select one stable ID for Codex-account chats.

    An existing reserved ID with different connection/auth settings cannot be
    overwritten: persisted chats may still reference it.
    """
    providers = data.get("model_providers", {})
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        effective = {**base.get("model_providers", {}).get(provider_id, {}), **provider}
        if codex_auth(effective):
            document["model_providers"][provider_id]["name"] = "OpenAI"
    selected = data.get("model_provider", base.get("model_provider", "openai"))
    source = {**base.get("model_providers", {}).get(selected, {}), **providers.get(selected, {})}
    if selected == "openai" and native_upstream.endswith("/backend-api/codex"):
        source = {"name": "OpenAI", "wire_api": "responses", "requires_openai_auth": True,
                  "base_url": data.get("openai_base_url", base.get("openai_base_url", native_upstream))}
    if not codex_auth(source):
        return
    if not primary and "model_provider" not in data and selected not in providers:
        return
    source_url = original_upstream(source.get("base_url", native_upstream), selected)
    # An independent profile may intentionally override the global connection.
    # Only an ID already defined in this file can collide with its new default.
    existing = providers.get(PROVIDER, {})
    if existing and selected != PROVIDER:
        ignored = {"name", "base_url", "supports_websockets"}
        def identity(value):
            return {k: v for k, v in {"wire_api": "responses", **value}.items() if k not in ignored}
        if (identity(existing) != identity(source)
                or original_upstream(existing.get("base_url", native_upstream), PROVIDER) != source_url):
            raise ValueError("现有 codex_packet_inspector 与所选 provider 的上游或认证不同，未覆盖已有会话的连接配置。")
    if selected != PROVIDER:
        canonical = deepcopy(source)
        canonical.setdefault("wire_api", "responses")
        canonical.update(name="OpenAI", base_url=source_url, supports_websockets=False)
        document.setdefault("model_providers", {})[PROVIDER] = canonical
        document["model_provider"] = PROVIDER
    native_compression(document, False)
