"""Reversible native-image-tool replacement using standard Codex config layers."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import subprocess
import tomllib

import tomlkit

from .config_link import ConfigLink
from .locking import file_lock
from .image_tool_install import ImageToolInstallation
from .codex_runtime import find_codex_cli

TOOL_SERVER = "isolated_vision"
VISION_PROVIDER = "codex_isolated_vision"
VISION_PROFILE = "isolated-vision"
WORKER_HEADER = "X-Codex-Isolated-Vision"
IMAGE_MODES = ("native", "disabled", "optimized")


def image_mode(settings):
    mode = settings.get("image_analysis_mode")
    if isinstance(mode, str) and mode in IMAGE_MODES:
        return mode
    return "optimized" if settings.get("image_isolation_enabled") is True else "native"


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
    def __init__(self, link, directory, relay_endpoint):
        self.link = link
        self.directory = Path(directory)
        self.path = self.directory / "image-tool.json"
        self.profile_path = link.config.with_name(VISION_PROFILE + ".config.toml")
        self.relay_endpoint = relay_endpoint
        self.installation = ImageToolInstallation(link.config.parent)
        self.state = json.loads(self.path.read_text(encoding="utf8")) if self.path.exists() else {}
        if self.state and self.state.get("config") != str(link.config.resolve()):
            raise ValueError("图片工具配置属于另一个 Codex 配置文件。")

    def is_worker(self, value):
        token = self.state.get("worker_token", "")
        return bool(token and value and secrets.compare_digest(value.encode("utf8"), token.encode("ascii")))

    def status(self):
        return {"enabled": bool(self.state.get("enabled")), "mode": self.mode(), "server": TOOL_SERVER,
                "native_tool": "view_image", "existing_tool_preserved": self.state.get("existing_tool_preserved", False),
                "managed_tool_installed": bool(self.state.get("enabled") and self.state.get("managed_mcp")),
                "transport": "stdio" if (self.state.get("managed_mcp") or {}).get("command") else None,
                "installation_error": getattr(self, "installation_error", ""),
                "reload_required": bool(self.state), "backup": self.state.get("backup", ""),
                "profile_path": str(self.profile_path)}

    def mode(self):
        return self.state.get("mode", "optimized" if self.state.get("enabled") else "native")

    def _disable_tools(self, document, data, previous):
        tools = data.get("tools", {})
        features = data.get("features", {})
        mcp = data.get("mcp_servers", {}).get(TOOL_SERVER)
        original = {"native_present": "view_image" in tools, "native": tools.get("view_image"),
                    "native_feature_present": "view_image" in features, "native_feature": features.get("view_image"),
                    "features_created": "features" not in data,
                    "tools_created": "tools" not in data, "mcp_present": mcp is not None,
                    "mcp_enabled_present": mcp is not None and "enabled" in mcp,
                    "mcp_enabled": mcp.get("enabled") if mcp is not None else None}
        if "tools" not in document:
            document["tools"] = tomlkit.table()
        document["tools"]["view_image"] = False
        if "features" not in document:
            document["features"] = tomlkit.table()
        document["features"]["view_image"] = False
        if mcp is not None:
            document["mcp_servers"][TOOL_SERVER]["enabled"] = False
        return {**previous, "schema_version": 3, "config": str(self.link.config.resolve()),
                "enabled": False, "mode": "disabled", "disabled_original": original}

    def _restore_disabled(self, document, data, previous):
        original = previous["disabled_original"]
        if data.get("tools", {}).get("view_image") is not False:
            raise ValueError("原生图片工具配置已被修改，未覆盖用户配置。")
        if "native_feature_present" in original:
            if data.get("features", {}).get("view_image") is not False:
                raise ValueError("原生图片工具开关已被修改，未覆盖用户配置。")
            document["features"].pop("view_image", None)
            if original["native_feature_present"]:
                document["features"]["view_image"] = original["native_feature"]
            if original["features_created"] and not document["features"]:
                document.pop("features")
        if original["mcp_present"]:
            mcp = data.get("mcp_servers", {}).get(TOOL_SERVER, {})
            if mcp.get("enabled") is not False:
                raise ValueError("独立图片工具配置已被修改，未覆盖用户配置。")
            target = document["mcp_servers"][TOOL_SERVER]
            target.pop("enabled", None)
            if original["mcp_enabled_present"]:
                target["enabled"] = original["mcp_enabled"]
        document["tools"].pop("view_image", None)
        if original["native_present"]:
            document["tools"]["view_image"] = original["native"]
        if original["tools_created"] and not document["tools"]:
            document.pop("tools")

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
        registration = self.installation.install(cli) if old_tool is None else None
        fields = [("tools", "view_image"), ("features", "view_image")]
        if TOOL_SERVER not in data.get("mcp_servers", {}):
            fields.append(("mcp_servers", TOOL_SERVER))
        originals = []
        for table, name in fields:
            values = data.get(table, {})
            originals.append({"table": table, "name": name, "present": name in values, "value": values.get(name)})
            if table not in document:
                document[table] = tomlkit.table()
        document["tools"]["view_image"] = False
        document["features"]["view_image"] = False
        if old_tool is None:
            document["mcp_servers"][TOOL_SERVER] = registration
        # Tool isolation uses a profile that inherits the current connection.
        # Existing selectors/providers, including inline profiles, belong to
        # connection management and are never rewritten by image installation.
        inline_profile = VISION_PROFILE in data.get("profiles", {})
        created = not profile_existed and not inline_profile
        profile_updated = tomlkit.dumps(profile_document) if profile_existed or created else None
        candidate = {"schema_version": 5, "config": str(self.link.config.resolve()), "enabled": True,
            "cli": str(cli), "originals": originals,
            "managed_mcp": registration if old_tool is None else None,
            "created_tables": sorted({table for table, _ in fields if table not in data}),
            "existing_tool_preserved": old_tool is not None, "profile_existed": profile_existed,
            "profile_created": created, "managed_profile": dict(profile_data)}
        return profile_updated, candidate

    def _refresh_managed_tool(self):
        """Migrate our HTTP registration and update installed stdio assets."""
        before, data, config_raw = read_configuration(self.link.config)
        current = data.get("mcp_servers", {}).get(TOOL_SERVER)
        managed = self.state.get("managed_mcp")
        adopted = not managed and self.installation.matches(current)
        feature_owned = any(v["table"] == "features" and v["name"] == "view_image"
                            for v in self.state.get("originals", []))
        if feature_owned and data.get("features", {}).get("view_image") is not False:
            raise ValueError("原生图片工具开关已被修改，未覆盖用户配置。")
        manage_registration = bool(managed or adopted)
        if not manage_registration and feature_owned:
            return False
        if managed and current != managed:
            raise ValueError("独立图片工具注册已被修改，未覆盖用户配置。")
        cli = self.state.get("cli")
        registration = current
        if manage_registration:
            if not cli or not Path(cli).is_file():
                cli = find_codex_cli()
            if not cli:
                raise ValueError("未找到 Codex CLI，无法更新独立图片工具。请先安装或打开 Codex 桌面版后重试。")
            registration = self.installation.install(cli)
        if (managed == registration and self.state.get("schema_version") == 5
                and self.state.get("cli") == str(cli) and feature_owned):
            return False
        document = tomlkit.parse(before)
        if manage_registration:
            document["mcp_servers"][TOOL_SERVER] = registration
        previous = deepcopy(self.state)
        state_raw = self.path.read_bytes() if self.path.exists() else None
        candidate = deepcopy(previous)
        if manage_registration:
            candidate.update(schema_version=5, cli=str(cli), managed_mcp=registration, existing_tool_preserved=False)
        candidate.pop("mcp_token", None)
        if adopted and not any(v["table"] == "mcp_servers" and v["name"] == TOOL_SERVER for v in candidate["originals"]):
            candidate["originals"] = [*candidate["originals"],
                {"table": "mcp_servers", "name": TOOL_SERVER, "present": False, "value": None}]
        if not feature_owned:
            features = data.get("features", {})
            candidate["originals"] = [*candidate.get("originals", []),
                {"table": "features", "name": "view_image", "present": "view_image" in features,
                 "value": features.get("view_image")}]
            if "features" not in document:
                document["features"] = tomlkit.table()
                candidate["created_tables"] = [*candidate.get("created_tables", []), "features"]
            document["features"]["view_image"] = False
        updated = tomlkit.dumps(document)
        written = False
        try:
            if updated != before:
                candidate["backup"] = self.link._write(updated)
                written = True
            if manage_registration:
                self._validate(candidate)
            self._save(candidate)
            return True
        except BaseException:
            if written:
                if read_configuration(self.link.config)[0] != updated:
                    raise ValueError("图片工具迁移期间配置被修改，未覆盖用户更改。") from None
                restore_file(self.link.config, config_raw)
            restore_file(self.path, state_raw)
            self.state = previous
            raise

    def _disable(self, document, data, profile_document, profile_data, previous):
        if previous.get("managed_mcp") and data.get("mcp_servers", {}).get(TOOL_SERVER) != previous["managed_mcp"]:
            raise ValueError("独立图片工具注册已被修改，未覆盖用户配置。")
        if data.get("tools", {}).get("view_image") is not False:
            raise ValueError("原生图片工具配置已被修改，未覆盖用户配置。")
        if (any(v["table"] == "features" and v["name"] == "view_image" for v in previous["originals"])
                and data.get("features", {}).get("view_image") is not False):
            raise ValueError("原生图片工具开关已被修改，未覆盖用户配置。")
        for original in previous["originals"]:
            table = original["table"]
            if table not in {"tools", "features", "mcp_servers"}:
                continue  # Legacy provider records now belong to connection management.
            if table not in document:
                document[table] = tomlkit.table()
            target = document[table]
            target.pop(original["name"], None)
            if original["present"]:
                target[original["name"]] = original["value"]
        for table in previous.get("created_tables", []):
            if table in {"tools", "features", "mcp_servers"} and not document.get(table):
                document.pop(table, None)
        profile_updated = tomlkit.dumps(profile_document) if self.profile_path.exists() else None
        if (previous.get("schema_version", 0) >= 5 and previous.get("profile_created")
                and profile_data == previous.get("managed_profile")):
            profile_updated = None
        return profile_updated, {**previous, "enabled": False}

    @contextmanager
    def change(self, mode):
        """Rollback root config, profile layer, state, and caller settings together."""
        if isinstance(mode, bool):
            mode = "optimized" if mode else "native"
        if not isinstance(mode, str) or mode not in IMAGE_MODES:
            raise ValueError("invalid image analysis mode")
        with file_lock(self.directory / "image-tool.lock"), self.installation.transaction():
            previous = deepcopy(self.state)
            previous_mode = self.mode()
            if previous_mode == mode == "native":
                _, data, _ = read_configuration(self.link.config)
                if (data.get("tools", {}).get("view_image") is not False
                        and data.get("features", {}).get("view_image") is not False
                        and not self.installation.manifest.exists()):
                    yield False
                    return
            if (previous_mode == mode and mode != "native"
                    and (mode != "disabled" or "native_feature_present" in previous.get("disabled_original", {}))):
                if mode == "optimized":
                    _, data, _ = read_configuration(self.link.config)
                    _, profile_data, _ = read_configuration(self.profile_path)
                    if data.get("tools", {}).get("view_image") is not False:
                        raise ValueError("图片工具配置已被修改，请先检查 Codex 配置。")
                elif mode == "disabled":
                    before, data, _ = read_configuration(self.link.config)
                    self._restore_disabled(tomlkit.parse(before), data, previous)
                config_raw = self.link.config.read_bytes() if self.link.config.exists() else None
                state_raw = self.path.read_bytes() if self.path.exists() else None
                changed = False
                applied = None
                try:
                    changed = self._refresh_managed_tool() if mode == "optimized" else False
                    applied = self.link.config.read_bytes() if self.link.config.exists() else None
                    self.installation_error = ""
                    yield changed
                except BaseException:
                    if changed:
                        if self.link.config.read_bytes() != applied:
                            raise ValueError("图片工具迁移期间配置被修改，未覆盖用户更改。") from None
                        restore_file(self.link.config, config_raw)
                        restore_file(self.path, state_raw)
                        self.state = previous
                    raise
                return
            before, data, config_before = read_configuration(self.link.config)
            profile_before, profile_data, profile_raw = read_configuration(self.profile_path)
            document, profile_document = tomlkit.parse(before), tomlkit.parse(profile_before)
            profile_updated = profile_before if profile_raw is not None else None
            if previous_mode == "optimized":
                profile_updated, _ = self._disable(document, data, profile_document, profile_data, previous)
                profile_document = tomlkit.parse(profile_updated or "")
            elif previous_mode == "disabled":
                self._restore_disabled(document, data, previous)
            data = tomllib.loads(tomlkit.dumps(document))
            profile_data = tomllib.loads(profile_updated or "")
            if mode == "optimized":
                profile_updated, candidate = self._enable(document, data, profile_document, profile_data,
                                                           profile_updated is not None, previous)
                candidate["mode"] = mode
            elif mode == "disabled":
                candidate = self._disable_tools(document, data, previous)
            else:
                candidate = {**previous, "schema_version": 5, "profile_created": False, "config": str(self.link.config.resolve()),
                             "enabled": False, "mode": "native"}
                if "tools" not in document:
                    document["tools"] = tomlkit.table()
                document["tools"]["view_image"] = True
                if document.get("features", {}).get("view_image") is False:
                    document["features"].pop("view_image")
                    if not document["features"]:
                        document.pop("features")
            updated = tomlkit.dumps(document)
            tomllib.loads(updated)
            state_before = self.path.read_bytes() if self.path.exists() else None
            written = profile_written = False
            try:
                candidate["backup"] = self.link._write(updated)
                written = True
                if profile_updated != (profile_before if profile_raw is not None else None):
                    if profile_updated is None:
                        self.profile_path.unlink(missing_ok=True)
                    else:
                        candidate["profile_backup"] = ConfigLink(self.profile_path, self.path)._write(profile_updated)
                    profile_written = True
                if mode == "optimized":
                    self._validate(candidate)
                self._save(candidate)
                self.installation_error = ""
                if mode != "optimized":
                    self.installation.uninstall()
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
