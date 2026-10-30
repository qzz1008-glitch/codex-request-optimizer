"""Reversible standard provider selection; never changes native login data."""
import json
from pathlib import Path
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
        # Isolated image analysis is launched with its own native profile. Route
        # that profile through B too, even if it has an explicit old provider.
        from .image_config import VISION_PROFILE
        names = {VISION_PROFILE}
        if isinstance(profile, str):
            names.add(profile)
        state["profiles"] = [{"name": name, "selected": values.get("model_provider")}
                             for name, values in data.get("profiles", {}).items() if name in names]
        document = tomlkit.parse(text)
        document["model_provider"] = SHARED_PROVIDER
        document.setdefault("model_providers", {})[SHARED_PROVIDER] = provider
        for item in state["profiles"]:
            document["profiles"][item["name"]]["model_provider"] = SHARED_PROVIDER
        layers, plans = [], [(self.link.config, text, tomlkit.dumps(document))]
        for name in sorted(names):
            # Profile selectors must never become arbitrary filesystem paths.
            if not name or name in {".", ".."} or any(char in name for char in '/\\:'):
                continue
            path = self.link.config.with_name(name + ".config.toml")
            if not path.is_file():
                continue
            layer_link = type(self.link)(path, self.path)
            before, layer_data = layer_link._read()
            if SHARED_PROVIDER in layer_data.get("model_providers", {}):
                raise SharingError("共享 provider 名称已被占用，请先检查 Codex 配置。")
            layer = tomlkit.parse(before)
            entries = [{"name": key, "selected": values.get("model_provider")}
                       for key, values in layer_data.get("profiles", {}).items() if key in names]
            layers.append({"name": path.name, "selected": layer_data.get("model_provider"), "profiles": entries})
            layer["model_provider"] = SHARED_PROVIDER
            for item in entries:
                layer["profiles"][item["name"]]["model_provider"] = SHARED_PROVIDER
            plans.append((path, before, tomlkit.dumps(layer)))
        state["layers"] = layers
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".new")
        staging.write_text(json.dumps(state), encoding="utf8")
        staging.replace(self.path)
        try:
            from .providers import _write_plans
            _write_plans(self.link, plans)
        except Exception:
            # A concurrent edit may prevent rollback. Keep recovery metadata
            # unless every file has returned to its exact original state.
            if all(path.exists() and path.read_text(encoding="utf-8-sig") == before
                   for path, before, _updated in plans):
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
        profiles = state.get("profiles", [state["profile"]] if state.get("profile") else [])
        for profile in profiles:
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
        plans = [(self.link.config, text, tomlkit.dumps(document))] if changed else []
        for layer in state.get("layers", []):
            name = layer["name"]
            if not isinstance(name, str) or Path(name).name != name or any(char in name for char in '/\\:') or not name.endswith(".config.toml"):
                raise SharingError("共享配置恢复记录无效，请检查 Codex 配置。")
            path = self.link.config.with_name(name)
            if not path.is_file():
                continue
            before, _ = type(self.link)(path, self.path)._read()
            target = tomlkit.parse(before)
            for values, prior in [(target, layer), *((target.get("profiles", {}).get(p["name"], {}), p)
                                                   for p in layer.get("profiles", []))]:
                if values.get("model_provider") == SHARED_PROVIDER:
                    if prior["selected"] is None:
                        del values["model_provider"]
                    else:
                        values["model_provider"] = prior["selected"]
            after = tomlkit.dumps(target)
            if after != before:
                plans.append((path, before, after))
        if plans:
            from .providers import _write_plans
            _write_plans(self.link, plans)
        self.path.unlink(missing_ok=True)
        return bool(plans)

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
