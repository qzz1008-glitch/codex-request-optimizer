"""Reversible provider linking and explicit context configuration edits."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tomllib
from urllib.parse import urlsplit

from .native_state import read_state, state_databases

PROVIDER = "codex_packet_inspector"
BEGIN = "# BEGIN CODEX PACKET INSPECTOR"
END = "# END CODEX PACKET INSPECTOR"


def validate_upstream(url):
    invalid = "上游地址须为不含密码、查询参数的 HTTP/HTTPS 地址"
    if not isinstance(url, str):
        raise ValueError(invalid)
    try:
        p = urlsplit(url)
        _ = p.port  # Reject malformed/out-of-range ports before changing a config file.
    except ValueError:
        raise ValueError(invalid) from None
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError(invalid)
    if p.scheme == "http" and p.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("非本机的上游必须使用 HTTPS")
    return url.rstrip("/")


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + toml_value(v) for k, v in value.items()) + " }"
    raise ValueError("不支持的 provider 配置类型")


class ConfigLink:
    def __init__(self, config: Path, state_path: Path):
        self.config = config
        self.state_path = state_path

    def _read(self):
        text = self.config.read_text(encoding="utf-8-sig") if self.config.exists() else ""
        return text, tomllib.loads(text)

    def context_settings(self):
        _, data = self._read()
        return self._context_settings(data)

    def _context_settings(self, data):
        model = data.get("model", "")
        model_info = {}
        catalog = data.get("model_catalog_json")
        catalog_path = Path(catalog).expanduser() if catalog else self.config.parent / "models_cache.json"
        if not catalog_path.is_absolute():
            catalog_path = self.config.parent / catalog_path
        try:
            cache = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
            models = cache.get("models", []) if isinstance(cache, dict) else cache
            model_info = next((m for m in models if isinstance(m, dict) and m.get("slug", m.get("id")) == model), {})
        except (OSError, ValueError, TypeError):
            pass
        def positive_int(value):
            return value if type(value) is int and value > 0 else None
        percent = positive_int(model_info.get("effective_context_window_percent")) or 95
        if percent > 100:
            percent = 95
        configured = positive_int(data.get("model_context_window"))
        default = positive_int(model_info.get("context_window"))
        maximum = positive_int(model_info.get("max_context_window")) or default
        return {"model": model, "config_path": str(self.config), "effective_percent": percent,
                "context_tokens": configured * percent // 100 if configured else None,
                "compact_tokens": positive_int(data.get("model_auto_compact_token_limit")),
                "default_context_tokens": default * percent // 100 if default else None,
                "max_context_tokens": maximum * percent // 100 if maximum else 10_000_000}

    @staticmethod
    def _set_root_integer(text, data, key, value):
        expected = dict(data)
        if value is None:
            expected.pop(key, None)
        else:
            expected[key] = value
        if expected == data:
            return text
        if key not in data:
            updated = key + " = " + str(value) + "\n" + text
            if tomllib.loads(updated) == expected:
                return updated
        # Verify the entire parsed result to avoid matching a table field or
        # an example assignment embedded in a multiline instruction string.
        name = re.escape(key)
        pattern = rf'''^([ \t]*(?:{name}|"{name}"|'{name}')[ \t]*=[ \t]*)([-+]?\w+)([ \t]*(?:#[^\r\n]*)?)(\r?\n|$)'''
        for match in re.finditer(pattern, text, re.M):
            if value is None:
                comment = match[3].lstrip()
                replacement = comment + match[4] if comment else ""
            else:
                replacement = match[1] + str(value) + match[3] + match[4]
            updated = text[:match.start()] + replacement + text[match.end():]
            try:
                if tomllib.loads(updated) == expected:
                    return updated
            except tomllib.TOMLDecodeError:
                pass
        raise ValueError("无法安全修改 " + key + "，请检查该项的 TOML 写法")

    def save_context_settings(self, values):
        if not isinstance(values, dict) or set(values) != {"context_tokens", "compact_tokens"}:
            raise ValueError("请同时提供上下文窗口和自动压缩阈值；null 表示模型默认")
        text, data = self._read()
        info = self._context_settings(data)
        context, compact = values["context_tokens"], values["compact_tokens"]
        for key, value, minimum in (("上下文窗口", context, 4000), ("压缩阈值", compact, 1000)):
            if value is not None and (type(value) is not int or not minimum <= value <= 10_000_000):
                raise ValueError(key + "须为有效的整数 tokens")
        if context is not None and context > info["max_context_tokens"]:
            raise ValueError("上下文窗口超过当前模型支持的上限")
        effective = context if context is not None else info["default_context_tokens"]
        if compact is not None and effective is not None and compact > effective:
            raise ValueError("自动压缩阈值不能大于有效上下文窗口")
        percent = info["effective_percent"]
        raw_context = (context * 100 + percent - 1) // percent if context is not None else None
        updated = self._set_root_integer(text, data, "model_context_window", raw_context)
        updated_data = tomllib.loads(updated)
        updated = self._set_root_integer(updated, updated_data, "model_auto_compact_token_limit", compact)
        backup = self._write(updated) if updated != text else ""
        return {**self._context_settings(tomllib.loads(updated)), "backup": backup,
                "changed": updated != text, "reload_required": updated != text}

    def _state(self):
        return json.loads(self.state_path.read_text(encoding="utf8")) if self.state_path.exists() else {}

    def profile(self):
        text, data = self._read()
        selected = data.get("model_provider", "openai")
        state = self._state()
        all_providers = state.get("mode") == "all-providers-v1"
        active = selected == PROVIDER and bool(state) and BEGIN in text
        provider = data.get("model_providers", {}).get(selected, {})
        upstream = state.get("upstream", "") if active else provider.get("base_url", "")
        if all_providers:
            keys = ["openai_base_url"] if selected == "openai" else ["model_providers", selected, "base_url"]
            records = state.get("files", {}).get(str(self.config.resolve()), [])
            record = next((r for r in records if r["keys"] == keys), None)
            value = data.get("openai_base_url") if selected == "openai" else provider.get("base_url")
            active = bool(record and value == record["applied"])
            upstream = record.get("before") if active else value
            if active and selected == PROVIDER:
                upstream = state.get("legacy", {}).get("upstream") or upstream
        if not upstream:
            auth_file = self.config.parent / "auth.json"
            auth_mode = "chatgpt"
            if auth_file.is_file():
                try:
                    auth_mode = json.loads(auth_file.read_text(encoding="utf8")).get("auth_mode", "chatgpt")
                except (OSError, ValueError):
                    pass
            upstream = "https://api.openai.com/v1" if auth_mode == "apikey" else "https://chatgpt.com/backend-api/codex"
        return {"config_path": str(self.config), "provider": selected, "active": active,
                "base_url": data.get("openai_base_url", "") if selected == "openai" else provider.get("base_url", ""),
                "upstream": validate_upstream(upstream), "can_restore": active and bool(state),
                "all_providers": all_providers,
                "websockets_supported_by_capture": False}

    def _write(self, text):
        tomllib.loads(text)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if self.config.exists():
            backup = self.config.with_name(self.config.name + ".inspector-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            shutil.copy2(self.config, backup)
        staging = self.config.with_name(self.config.name + ".inspector-new")
        staging.write_text(text, encoding="utf8", newline="\n")
        staging.replace(self.config)
        return str(backup) if backup else ""

    @staticmethod
    def _select(text, provider):
        first_table = re.search(r"^[ \t]*\[", text, re.M)
        split = first_table.start() if first_table else len(text)
        head, tail = text[:split], text[split:]
        line = "model_provider = " + toml_value(provider) + "\n" if provider else ""
        pattern = r'''^[ \t]*(?:model_provider|"model_provider"|'model_provider')[ \t]*=.*(?:\n|$)'''
        if re.search(pattern, head, re.M):
            head = re.sub(pattern, lambda _: line, head, count=1, flags=re.M)
        elif line:
            head = line + head
        return head + tail

    def activate(self, endpoint, upstream):
        text, data = self._read()
        selected = data.get("model_provider", "openai")
        if selected == PROVIDER:
            raise ValueError("已经接入采集器；如需更换先恢复原出口")
        if PROVIDER in data.get("model_providers", {}) or BEGIN in text:
            raise ValueError("已有同名 provider，未覆盖")
        upstream = validate_upstream(upstream)
        p = urlsplit(upstream)
        e = urlsplit(endpoint)
        if p.hostname in {"127.0.0.1", "localhost", "::1"} and p.port == e.port:
            raise ValueError("上游不能指向采集器自身")
        original = data.get("model_providers", {}).get(selected)
        provider = dict(original) if original is not None else {
            "name": "OpenAI", "wire_api": "responses", "requires_openai_auth": True}
        provider.update(base_url=endpoint, supports_websockets=False)
        block = "\n" + BEGIN + "\n[model_providers." + PROVIDER + "]\n"
        block += "".join(json.dumps(k) + " = " + toml_value(v) + "\n" for k, v in provider.items())
        block += END + "\n"
        updated = self._select(text, PROVIDER).rstrip() + "\n" + block
        tomllib.loads(updated)
        state = {"previous_provider": data.get("model_provider"), "upstream": upstream,
                 "backup": ""}
        # Recovery information must exist before the Codex config can point here.
        prior_state = self.state_path.read_bytes() if self.state_path.exists() else None
        self._write_state(state)
        try:
            backup = self._write(updated)
        except Exception:
            if prior_state is None:
                self.state_path.unlink(missing_ok=True)
            else:
                self.state_path.write_bytes(prior_state)
            raise
        state["backup"] = backup
        try:
            self._write_state(state)
        except OSError:
            pass  # The already-saved previous provider still permits restoration.
        return {"active": True, "backup": backup, "reload_required": True}

    def _write_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.state_path.with_suffix(".new")
        staging.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
        staging.replace(self.state_path)

    def _provider_is_referenced(self, provider_id, data):
        def references(document):
            return any(profile.get("model_provider") == provider_id
                       for profile in document.get("profiles", {}).values())
        if references(data):
            return True
        for profile in self.config.parent.glob("*.config.toml"):
            if profile.resolve() == self.config.resolve():
                continue
            try:
                document = tomllib.loads(profile.read_text(encoding="utf-8-sig"))
                if document.get("model_provider") == provider_id or references(document):
                    return True
            except (OSError, ValueError):
                return True
        databases = state_databases(self.config.parent)
        uncertain = not databases and (self.config.parent / "sessions").is_dir()
        for database in databases:
            try:
                with read_state(database, timeout=1) as connection:
                    if connection.execute("SELECT 1 FROM threads WHERE model_provider=? LIMIT 1", (provider_id,)).fetchone():
                        return True
            except (OSError, sqlite3.Error):
                # An unavailable index cannot prove that a provider is unused.
                uncertain = True
        return uncertain

    def restore(self):
        text, data = self._read()
        state = self._state()
        if state.get("mode") == "all-providers-v1":
            from .providers import restore_providers
            return restore_providers(self, state)
        if not state:
            raise ValueError("当前配置不是本采集器接入的状态，未修改")
        pattern = r"\n?" + re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?"
        if len(re.findall(pattern, text, re.S)) != 1:
            raise ValueError("接入块已被改动，请使用备份手动核对")
        text = re.sub(pattern, "\n", text, flags=re.S)
        if data.get("model_provider") == PROVIDER:
            text = self._select(text, state.get("previous_provider"))
        retained = self._provider_is_referenced(PROVIDER, data)
        if retained:
            provider = dict(data["model_providers"][PROVIDER])
            provider.update(base_url=validate_upstream(state["upstream"]), supports_websockets=False)
            text = text.rstrip() + "\n\n# Compatibility provider for saved Codex sessions.\n"
            text += "[model_providers." + PROVIDER + "]\n"
            text += "".join(json.dumps(key) + " = " + toml_value(value) + "\n" for key, value in provider.items())
        backup = self._write(text)
        self._write_state({})
        return {"active": False, "backup": backup, "reload_required": True, "retained_provider": retained}
