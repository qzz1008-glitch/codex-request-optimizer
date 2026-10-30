"""Reversible standard provider selection; never changes native login data."""
import json
import tomlkit

from .sharing_auth import SharingError

SHARED_PROVIDER = "codex_inspector_shared"
RETRY_KEYS = {"stream_max_retries", "request_max_retries"}


class SharedProvider:
    def __init__(self, link, state_path):
        self.link, self.path = link, state_path

    def activate(self, endpoint, retry_limit=None):
        if self.path.exists():
            self.restore()
        text, data = self.link._read()
        if SHARED_PROVIDER in data.get("model_providers", {}):
            raise SharingError("共享 provider 名称已被占用，请先检查 Codex 配置。")
        provider = {"name": "Codex shared connection", "base_url": endpoint, "wire_api": "responses",
                    "requires_openai_auth": False, "supports_websockets": False}
        if retry_limit is not None:
            provider.update(stream_max_retries=retry_limit, request_max_retries=0)
        state = {"selected": data.get("model_provider"), "provider": provider}
        profile = data.get("profile")
        profile_data = data.get("profiles", {}).get(profile, {}) if isinstance(profile, str) else {}
        if profile_data:
            state["profile"] = {"name": profile, "selected": profile_data.get("model_provider")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".new")
        staging.write_text(json.dumps(state), encoding="utf8")
        staging.replace(self.path)
        document = tomlkit.parse(text)
        document["model_provider"] = SHARED_PROVIDER
        document.setdefault("model_providers", {})[SHARED_PROVIDER] = provider
        if "profile" in state:
            document["profiles"][profile]["model_provider"] = SHARED_PROVIDER
        try:
            if self.link._read()[0] != text:
                raise SharingError("Codex 配置已发生变化，请重试。")
            self.link._write(tomlkit.dumps(document))
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    def restore(self):
        if not self.path.exists():
            return False
        state = json.loads(self.path.read_text(encoding="utf8"))
        text, _ = self.link._read()
        document = tomlkit.parse(text)
        changed = False
        if document.get("model_provider") == SHARED_PROVIDER:
            if state["selected"] is None:
                del document["model_provider"]
            else:
                document["model_provider"] = state["selected"]
            changed = True
        profile = state.get("profile")
        if profile:
            target = document.get("profiles", {}).get(profile["name"], {})
            if target.get("model_provider") == SHARED_PROVIDER:
                if profile["selected"] is None:
                    del target["model_provider"]
                else:
                    target["model_provider"] = profile["selected"]
                changed = True
        providers = document.get("model_providers", {})
        current = providers.get(SHARED_PROVIDER, {})
        if self._owned(current, state["provider"]):
            del providers[SHARED_PROVIDER]
            changed = True
        if changed:
            if self.link._read()[0] != text:
                raise SharingError("Codex 配置已发生变化，请重试。")
            self.link._write(tomlkit.dumps(document))
        self.path.unlink(missing_ok=True)
        return changed

    @staticmethod
    def _owned(current, applied):
        return ({k: v for k, v in current.items() if k not in RETRY_KEYS}
                == {k: v for k, v in applied.items() if k not in RETRY_KEYS})

    def set_retry(self, limit):
        state = json.loads(self.path.read_text(encoding="utf8"))
        text, _ = self.link._read()
        document = tomlkit.parse(text)
        provider = document.get("model_providers", {}).get(SHARED_PROVIDER, {})
        if not self._owned(provider, state["provider"]):
            raise SharingError("共享 provider 已被修改，请断开后重新连接。")
        before = dict(provider)
        if limit is None:
            for key in RETRY_KEYS:
                provider.pop(key, None)
        else:
            provider.update(stream_max_retries=limit, request_max_retries=0)
        changed = before != dict(provider)
        if changed:
            if self.link._read()[0] != text:
                raise SharingError("Codex 配置已发生变化，请重试。")
            self.link._write(tomlkit.dumps(document))
        return changed
