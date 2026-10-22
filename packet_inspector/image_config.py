"""Reversible native-image-tool replacement using standard Codex config layers."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tomllib

import tomlkit

from .config_link import ConfigLink, validate_upstream
from .locking import file_lock

TOOL_SERVER = "isolated_vision"
VISION_PROVIDER = "codex_isolated_vision"
VISION_PROFILE = "isolated-vision"
WORKER_HEADER = "X-Codex-Isolated-Vision"


def find_codex_cli(configured=None):
    """Use an existing CLI, including the desktop app's per-user CLI cache."""
    selected = configured or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if selected:
        return str(selected)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    directory = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path for path in directory.glob("*/codex.exe") if path.is_file()]
        return str(max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()) if candidates else None
    except OSError:
        return None


def read_configuration(path):
    raw = path.read_bytes() if path.exists() else None
    text = raw.decode("utf-8-sig").replace("\r\n", "\n") if raw is not None else ""
    return text, tomllib.loads(text), raw


def restore_file(path, raw):
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        staging = path.with_name(path.name + ".image-rollback")
        staging.write_bytes(raw)
        staging.replace(path)


class ImageToolConfig:
    def __init__(self, link, directory, api_base, relay_endpoint):
        self.link = link
        self.directory = Path(directory)
        self.path = self.directory / "image-tool.json"
        self.profile_path = link.config.with_name(VISION_PROFILE + ".config.toml")
        self.api_base, self.relay_endpoint = api_base, relay_endpoint
        self.state = json.loads(self.path.read_text(encoding="utf8")) if self.path.exists() else {}
        if self.state and self.state.get("config") != str(link.config.resolve()):
            raise ValueError("图片工具配置属于另一个 Codex 配置文件。")

    def is_worker(self, value):
        token = self.state.get("worker_token", "")
        return bool(token and value and secrets.compare_digest(value.encode("utf8"), token.encode("ascii")))

    def mcp_path(self):
        return "/mcp/image/" + self.state.get("mcp_token", "disabled")

    def status(self):
        return {"enabled": bool(self.state.get("enabled")), "server": TOOL_SERVER,
                "native_tool": "view_image", "existing_tool_preserved": self.state.get("existing_tool_preserved", False),
                "managed_tool_installed": bool(self.state.get("enabled") and self.state.get("managed_mcp")),
                "reload_required": bool(self.state), "backup": self.state.get("backup", ""),
                "profile_path": str(self.profile_path)}

    def _save(self, state):
        staging = self.path.with_suffix(".new")
        staging.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
        staging.replace(self.path)
        self.state = state

    def _validate(self, candidate):
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.link.config.parent)
        try:
            result = subprocess.run([candidate["cli"], "--profile", VISION_PROFILE,
                "mcp", "get", TOOL_SERVER, "--json"], env=environment, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("无法验证 Codex CLI 的独立识图配置，原设置已恢复。") from None
        if result.returncode:
            raise ValueError("Codex CLI 不接受独立识图配置，请检查 CLI 版本；原设置已恢复。")

    def _enable(self, document, data, profile_document, profile_data, profile_existed, previous):
        old_tool = data.get("mcp_servers", {}).get(TOOL_SERVER)
        if old_tool and old_tool.get("enabled") is False:
            raise ValueError("现有独立图片工具已被禁用，请先恢复该工具。")
        cli = find_codex_cli((old_tool or {}).get("env", {}).get("CODEX_CLI_PATH"))
        if not cli:
            raise ValueError("未找到 Codex CLI，无法安装独立图片工具。请先安装或打开 Codex 桌面版，或安装 Codex CLI 后重试。")
        if (VISION_PROVIDER in data.get("model_providers", {}) or
                VISION_PROVIDER in profile_data.get("model_providers", {})):
            raise ValueError("独立识图 provider 名称已被占用，未修改原配置。")
        legacy = data.get("profiles", {}).get(VISION_PROFILE, {})
        if data.get("profile") is not None and data["profile"] != VISION_PROFILE:
            raise ValueError("请先迁移旧版 profile 选择器，再启用图片工具替换。")
        if data.get("profiles", {}).keys() - {VISION_PROFILE}:
            raise ValueError("请先迁移其他旧版 profiles 到独立配置文件，再启用图片工具替换。")
        if legacy and not profile_existed:
            profile_document = tomlkit.parse(tomlkit.dumps(legacy))
        effective = {**legacy, **profile_data}
        selected = effective.get("model_provider", data.get("model_provider", "openai"))
        provider = deepcopy(data.get("model_providers", {}).get(selected, {}))
        provider.update(deepcopy(profile_data.get("model_providers", {}).get(selected, {})))
        if provider.get("wire_api", "responses") != "responses":
            raise ValueError("当前 provider 不支持 Responses 图片解析。")
        provider.update(name="Codex isolated image reader", wire_api="responses", supports_websockets=False)
        provider["base_url"] = validate_upstream(provider.get("base_url") or
            effective.get("openai_base_url") or data.get("openai_base_url") or self.relay_endpoint())
        if selected == "openai":
            provider["requires_openai_auth"] = True
        worker_token = previous.get("worker_token") or secrets.token_urlsafe(32)
        mcp_token = previous.get("mcp_token") or secrets.token_urlsafe(32)
        for key in ("http_headers", "env_http_headers"):
            provider[key] = {k: v for k, v in provider.get(key, {}).items() if k.lower() != WORKER_HEADER.lower()}
        provider["http_headers"][WORKER_HEADER] = worker_token
        registration = {"url": self.api_base() + "/mcp/image/" + mcp_token, "tool_timeout_sec": 300}
        fields = [("tools", "view_image"), ("model_providers", VISION_PROVIDER)]
        if TOOL_SERVER not in data.get("mcp_servers", {}):
            fields.append(("mcp_servers", TOOL_SERVER))
        if VISION_PROFILE in data.get("profiles", {}):
            fields.append(("profiles", VISION_PROFILE))
        if "profile" in data:
            fields.append((None, "profile"))
        originals = []
        for table, name in fields:
            values = data.get(table, {}) if table else data
            originals.append({"table": table, "name": name, "present": name in values, "value": values.get(name)})
            if table and table not in document:
                document[table] = tomlkit.table()
        # Disable only the native entry. Keep a user's existing isolated tool unchanged.
        document["tools"]["view_image"] = False
        if old_tool is None:
            document["mcp_servers"][TOOL_SERVER] = registration
        document["model_providers"][VISION_PROVIDER] = provider
        if VISION_PROFILE in document.get("profiles", {}):
            document["profiles"].pop(VISION_PROFILE)
            if not document["profiles"]:
                document.pop("profiles")
        document.pop("profile", None)
        profile_document["model_provider"] = VISION_PROVIDER
        profile_updated = tomlkit.dumps(profile_document)
        candidate = {"schema_version": 2, "config": str(self.link.config.resolve()), "enabled": True,
            "cli": str(cli), "worker_token": worker_token, "mcp_token": mcp_token, "originals": originals,
            "managed_mcp": registration if old_tool is None else None, "managed_provider": provider,
            "created_tables": sorted({table for table, _ in fields if table and table not in data}),
            "existing_tool_preserved": old_tool is not None, "profile_existed": profile_existed,
            "profile_selector_present": "model_provider" in profile_data,
            "profile_selector_before": profile_data.get("model_provider"),
            "managed_profile": tomllib.loads(profile_updated)}
        return profile_updated, candidate

    def _disable(self, document, data, profile_document, profile_data, previous):
        if previous.get("managed_mcp") and data.get("mcp_servers", {}).get(TOOL_SERVER) != previous["managed_mcp"]:
            raise ValueError("独立图片工具注册已被修改，未覆盖用户配置。")
        if data.get("tools", {}).get("view_image") is not False:
            raise ValueError("原生图片工具配置已被修改，未覆盖用户配置。")
        provider = data.get("model_providers", {}).get(VISION_PROVIDER, {})
        comparable = lambda p: {k: v for k, v in p.items() if k not in {"base_url", "supports_websockets"}}
        if comparable(provider) != comparable(previous["managed_provider"]) or profile_data.get("model_provider") != VISION_PROVIDER:
            raise ValueError("独立识图配置已被修改，未覆盖用户配置。")
        for original in previous["originals"]:
            table = original["table"]
            if table and table not in document:
                document[table] = tomlkit.table()
            target = document[table] if table else document
            target.pop(original["name"], None)
            if original["present"]:
                target[original["name"]] = original["value"]
        for table in previous.get("created_tables", []):
            if not document.get(table):
                document.pop(table, None)
        if not previous["profile_existed"] and profile_data == previous["managed_profile"]:
            profile_updated = None
        else:
            profile_document.pop("model_provider", None)
            if previous["profile_selector_present"]:
                profile_document["model_provider"] = previous["profile_selector_before"]
            profile_updated = tomlkit.dumps(profile_document)
        return profile_updated, {**previous, "enabled": False}

    @contextmanager
    def change(self, enabled):
        """Rollback root config, profile layer, state, and caller settings together."""
        with file_lock(self.directory / "image-tool.lock"):
            previous = deepcopy(self.state)
            if bool(previous.get("enabled")) == enabled:
                if enabled:
                    _, data, _ = read_configuration(self.link.config)
                    _, profile_data, _ = read_configuration(self.profile_path)
                    if data.get("tools", {}).get("view_image") is not False or profile_data.get("model_provider") != VISION_PROVIDER:
                        raise ValueError("图片工具配置已被修改，请先检查 Codex 配置。")
                yield False
                return
            before, data, config_before = read_configuration(self.link.config)
            profile_before, profile_data, profile_raw = read_configuration(self.profile_path)
            document, profile_document = tomlkit.parse(before), tomlkit.parse(profile_before)
            if enabled:
                profile_updated, candidate = self._enable(document, data, profile_document, profile_data,
                                                           profile_raw is not None, previous)
            else:
                profile_updated, candidate = self._disable(document, data, profile_document, profile_data, previous)
            updated = tomlkit.dumps(document)
            tomllib.loads(updated)
            state_before = self.path.read_bytes() if self.path.exists() else None
            written = profile_written = False
            try:
                candidate["backup"] = self.link._write(updated)
                written = True
                if profile_updated is None:
                    self.profile_path.unlink(missing_ok=True)
                else:
                    candidate["profile_backup"] = ConfigLink(self.profile_path, self.path)._write(profile_updated)
                profile_written = True
                if enabled:
                    self._validate(candidate)
                self._save(candidate)
                yield True
            except Exception:
                if written and read_configuration(self.link.config)[0] != updated:
                    raise ValueError("配置发生并发修改；备份已保留，请检查后恢复。") from None
                if profile_written:
                    current = read_configuration(self.profile_path)
                    if (profile_updated is None and current[2] is not None) or (profile_updated is not None and current[0] != profile_updated):
                        raise ValueError("识图 profile 发生并发修改；备份已保留，请检查后恢复。") from None
                    restore_file(self.profile_path, profile_raw)
                if written:
                    restore_file(self.link.config, config_before)
                restore_file(self.path, state_before)
                self.state = previous
                raise
