"""Reversible, standard MCP/provider configuration for isolated image reading."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import secrets
import shutil
import tomllib

import tomlkit

from .config_link import validate_upstream
from .locking import file_lock

TOOL_SERVER = "isolated_vision"
VISION_PROVIDER = "codex_isolated_vision"
VISION_PROFILE = "isolated-vision"
WORKER_HEADER = "X-Codex-Isolated-Vision"


class ImageToolConfig:
    def __init__(self, link, directory, api_base, relay_endpoint):
        self.link = link
        self.directory = Path(directory)
        self.path = self.directory / "image-tool.json"
        self.api_base, self.relay_endpoint = api_base, relay_endpoint
        self.state = json.loads(self.path.read_text(encoding="utf8")) if self.path.exists() else {}
        if self.state and self.state.get("config") != str(link.config.resolve()):
            raise ValueError("图片工具配置属于另一个 Codex 配置文件。")

    def is_worker(self, value):
        token = self.state.get("worker_token", "")
        return bool(token and value
                    and secrets.compare_digest(value, token))

    def mcp_path(self):
        return "/mcp/image/" + self.state.get("mcp_token", "disabled")

    def status(self):
        return {"enabled": bool(self.state.get("enabled")), "server": TOOL_SERVER,
                "native_tool": "view_image", "existing_tool_preserved": self.state.get("existing_tool_preserved", False),
                "reload_required": bool(self.state), "backup": self.state.get("backup", "")}

    def _save(self, state):
        staging = self.path.with_suffix(".new")
        staging.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
        staging.replace(self.path)
        self.state = state

    @contextmanager
    def change(self, enabled):
        """Keep config and settings transactional; preserve unrelated TOML edits."""
        with file_lock(self.directory / "image-tool.lock"):
            previous = deepcopy(self.state)
            if bool(previous.get("enabled")) == enabled:
                if enabled:
                    _, data = self.link._read()
                    if data.get("tools", {}).get("view_image") is not False:
                        raise ValueError("原生图片工具配置已被修改，请先检查 Codex 配置。")
                yield False
                return
            config_before = self.link.config.read_bytes() if self.link.config.exists() else None
            before, data = self.link._read()
            document = tomlkit.parse(before)
            if enabled:
                old_tool = data.get("mcp_servers", {}).get(TOOL_SERVER)
                if old_tool and old_tool.get("enabled") is False:
                    raise ValueError("现有独立图片工具已被禁用，请先恢复该工具。")
                cli = (old_tool or {}).get("env", {}).get("CODEX_CLI_PATH") or shutil.which("codex")
                if not cli:
                    raise ValueError("未找到 Codex CLI，无法启用独立图片解析。")
                if VISION_PROVIDER in data.get("model_providers", {}):
                    raise ValueError("独立识图 provider 名称已被占用，未修改原配置。")
                selected = data.get("model_provider", "openai")
                provider = deepcopy(data.get("model_providers", {}).get(selected, {}))
                if provider.get("wire_api", "responses") != "responses":
                    raise ValueError("当前 provider 不支持 Responses 图片解析。")
                provider.update(name="Codex isolated image reader", wire_api="responses",
                                supports_websockets=False)
                provider["base_url"] = validate_upstream(provider.get("base_url") or
                    data.get("openai_base_url") or self.relay_endpoint())
                if selected == "openai":
                    provider["requires_openai_auth"] = True
                worker_token = previous.get("worker_token") or secrets.token_urlsafe(32)
                mcp_token = previous.get("mcp_token") or secrets.token_urlsafe(32)
                for key in ("http_headers", "env_http_headers"):
                    provider[key] = {k: v for k, v in provider.get(key, {}).items()
                                     if k.lower() != WORKER_HEADER.lower()}
                provider["http_headers"][WORKER_HEADER] = worker_token
                registration = {"url": self.api_base() + "/mcp/image/" + mcp_token,
                                "tool_timeout_sec": 300}
                originals = []
                fields = [("tools", "view_image"), ("model_providers", VISION_PROVIDER),
                          ("profiles", VISION_PROFILE)]
                if old_tool is None:
                    fields.append(("mcp_servers", TOOL_SERVER))
                for table, name in fields:
                    originals.append({"table": table, "name": name,
                                      "present": name in data.get(table, {}),
                                      "value": data.get(table, {}).get(name)})
                    if table not in document:
                        document[table] = tomlkit.table()
                # Disable Codex's native image tool; preserve an existing isolated_vision server.
                document["tools"]["view_image"] = False
                if old_tool is None:
                    document["mcp_servers"][TOOL_SERVER] = registration
                document["model_providers"][VISION_PROVIDER] = provider
                document["profiles"][VISION_PROFILE] = {"model_provider": VISION_PROVIDER}
                candidate = {"config": str(self.link.config.resolve()), "enabled": True,
                             "cli": str(cli), "worker_token": worker_token, "mcp_token": mcp_token,
                             "originals": originals, "managed_mcp": registration if old_tool is None else None,
                             "managed_provider": provider,
                             "created_tables": list({table for table, _ in fields if table not in data}),
                             "existing_tool_preserved": old_tool is not None}
            else:
                if previous.get("managed_mcp") and data.get("mcp_servers", {}).get(TOOL_SERVER) != previous["managed_mcp"]:
                    raise ValueError("独立图片工具注册已被修改，未覆盖用户配置。")
                if data.get("tools", {}).get("view_image") is not False:
                    raise ValueError("原生图片工具配置已被修改，未覆盖用户配置。")
                provider = data.get("model_providers", {}).get(VISION_PROVIDER, {})
                expected = previous["managed_provider"]
                # Provider auto-routing may legitimately change these two fields.
                comparable = lambda p: {k: v for k, v in p.items() if k not in {"base_url", "supports_websockets"}}
                if (comparable(provider) != comparable(expected) or
                    data.get("profiles", {}).get(VISION_PROFILE) != {"model_provider": VISION_PROVIDER}):
                    raise ValueError("独立识图配置已被修改，未覆盖用户配置。")
                for original in previous["originals"]:
                    table = document[original["table"]]
                    table.pop(original["name"], None)
                    if original["present"]:
                        table[original["name"]] = original["value"]
                for table in previous.get("created_tables", []):
                    if not document.get(table):
                        document.pop(table, None)
                candidate = {**previous, "enabled": False}
            updated = tomlkit.dumps(document)
            tomllib.loads(updated)
            state_before = self.path.read_bytes() if self.path.exists() else None
            written = False
            try:
                candidate["backup"] = self.link._write(updated)
                written = True
                self._save(candidate)
                yield True
            except Exception:
                if written:
                    if self.link.config.read_text(encoding="utf-8-sig") != updated:
                        raise ValueError("配置发生并发修改；原配置备份已保留，请检查后恢复。") from None
                    if config_before is None:
                        self.link.config.unlink(missing_ok=True)
                    else:
                        staging = self.link.config.with_name(self.link.config.name + ".image-rollback")
                        staging.write_bytes(config_before)
                        staging.replace(self.link.config)
                if state_before is None:
                    self.path.unlink(missing_ok=True)
                else:
                    staging = self.path.with_suffix(".rollback")
                    staging.write_bytes(state_before)
                    staging.replace(self.path)
                self.state = previous
                raise
