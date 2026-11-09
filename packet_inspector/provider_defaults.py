"""Codex's auth-dependent HTTP default; explicit provider URLs take precedence."""
import json
from pathlib import Path

API_UPSTREAM = "https://api.openai.com/v1"


def provider_auth(provider):
    if any(provider.get(key) for key in ("auth", "aws", "gateway_oauth")):
        return "external"
    if any(provider.get(key) for key in ("env_key", "experimental_bearer_token")):
        return "api"
    return "codex" if provider.get("requires_openai_auth") is True else "api"


def provider_upstream(provider, native):
    if "base_url" in provider:
        return provider["base_url"]
    kind = provider_auth(provider)
    return native if kind == "codex" else API_UPSTREAM if kind == "api" else None


def native_upstream(config):
    # Only inspect the login mode. Credentials remain owned by Codex.
    try:
        auth_text = (Path(config).parent / "auth.json").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        auth_text = ""
    try:
        data = json.loads(auth_text)
        mode = data.get("auth_mode", "chatgpt") if isinstance(data, dict) else "chatgpt"
    except ValueError:
        mode = "chatgpt"
    return API_UPSTREAM if mode == "apikey" else "https://chatgpt.com/backend-api/codex"
