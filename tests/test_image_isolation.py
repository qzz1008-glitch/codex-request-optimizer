"""Synthetic-only verification of native-tool replacement and image isolation."""
import base64
from copy import deepcopy
import gzip
import http.client
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from tests import test_packet_inspector as fixtures
import zstandard

from packet_inspector.analyzer import decode_body, encode
from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.image_config import ImageToolConfig, VISION_PROVIDER, WORKER_HEADER, find_codex_cli
from packet_inspector.image_isolation import ImageIsolation, ImageIsolationFailure
from packet_inspector.providers import ProviderManager
from packet_inspector.request_context import IMAGE_ANALYST_PREFIXES


def native_config(original):
    data = deepcopy(original)
    data.setdefault("tools", {})["view_image"] = True
    if data.get("features", {}).get("view_image") is False:
        data["features"].pop("view_image")
        if not data["features"]:
            data.pop("features")
    return data


def image_part():
    # This is a synthetic signature followed by dummy bytes, not a user image.
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-data" * 12
    return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(payload).decode()}


def request_body():
    return {"model": "test", "input": [
        {"role": "developer", "content": "Keep all original instructions."},
        {"type": "compaction", "encrypted_content": "unchanged-encrypted-state"},
        {"role": "user", "content": [{"type": "input_text", "text": "Read the error in this image."}, image_part()]}]}


def tool_image_body(custom=False):
    body = request_body()
    body["input"].extend([
        {"type": "custom_tool_call" if custom else "function_call", "id": "tool-call",
         "call_id": "image-call", "name": "exec" if custom else "image_tool",
         "input" if custom else "arguments": "original tool input"},
        {"type": "custom_tool_call_output" if custom else "function_call_output", "id": "tool-output",
         "call_id": "image-call", "output": [
             {"type": "input_text", "text": "Original tool result.", "extra": "preserve"}, image_part()],
         "internal_chat_message_metadata_passthrough": {"original": True}}])
    return body


class ImageCliDiscoveryTests(unittest.TestCase):
    def test_configured_cli_takes_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "configured.exe"
            configured.write_text("synthetic CLI placeholder", encoding="ascii")
            with patch.dict(os.environ, {"CODEX_CLI_PATH": "environment-cli"}), patch(
                    "packet_inspector.codex_runtime.shutil.which") as which:
                self.assertEqual(find_codex_cli(configured), str(configured.resolve()))
            which.assert_not_called()

    def test_desktop_only_user_does_not_need_cli_on_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "OpenAI" / "Codex" / "bin"
            older, newer = cache / "older" / "codex.exe", cache / "newer" / "codex.exe"
            for path in (older, newer):
                path.parent.mkdir(parents=True)
                path.write_text("synthetic CLI placeholder", encoding="ascii")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary, "CODEX_CLI_PATH": ""}), patch(
                    "packet_inspector.codex_runtime.shutil.which", return_value=None):
                self.assertEqual(find_codex_cli(), str(newer.resolve()))
                newer.unlink()
                self.assertEqual(find_codex_cli(), str(older.resolve()))

    def test_missing_desktop_and_cli_return_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"LOCALAPPDATA": temporary, "CODEX_CLI_PATH": ""}), patch(
                "packet_inspector.codex_runtime.shutil.which", return_value=None):
            self.assertIsNone(find_codex_cli())


class ImageConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "codex.exe"
        self.cli.write_text("synthetic executable placeholder", encoding="ascii")
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n[tools]\nweb_search=true\n'
            '[mcp_servers.isolated_vision]\ncommand="original-reader"\nargs=["existing.py"]\n'
            '[mcp_servers.other]\ncommand="unrelated"\n', encoding="utf8")
        self.manager = ImageToolConfig(ConfigLink(self.config, self.root / "link.json"), self.root,
            lambda: "http://127.0.0.1:43210/relay/test")

    def enable(self):
        with patch("packet_inspector.image_config.find_codex_cli", return_value=str(self.cli)), patch.object(
                self.manager, "_validate"), self.manager.change(True):
            pass

    def test_native_tool_disabled_existing_mcp_preserved_and_restored(self):
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.enable()
        configured = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertIs(configured["tools"]["view_image"], False)
        self.assertEqual(configured["mcp_servers"], original["mcp_servers"])
        self.assertNotIn("model_provider", tomllib.loads(self.manager.profile_path.read_text(encoding="utf8")))
        self.assertNotIn("isolated-vision", configured.get("profiles", {}))
        self.assertNotIn(VISION_PROVIDER, configured.get("model_providers", {}))
        self.assertTrue(Path(self.manager.status()["backup"]).is_file())
        self.assertFalse(self.manager.status()["managed_tool_installed"])
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), native_config(original))

    def test_disabled_mode_needs_no_cli_and_restores_both_tools(self):
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        with patch("packet_inspector.image_config.find_codex_cli", side_effect=AssertionError("no CLI needed")):
            with self.manager.change("disabled"):
                pass
        configured = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertIs(configured["tools"]["view_image"], False)
        self.assertIs(configured["mcp_servers"]["isolated_vision"]["enabled"], False)
        self.assertEqual(configured["mcp_servers"]["other"], original["mcp_servers"]["other"])
        self.assertFalse(self.manager.profile_path.exists())
        reloaded = ImageToolConfig(self.manager.link, self.root, self.manager.relay_endpoint)
        self.assertEqual(reloaded.status()["mode"], "disabled")
        with reloaded.change("native"):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), native_config(original))

    def test_all_mode_transitions_keep_the_original_configuration(self):
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        with patch("packet_inspector.image_config.find_codex_cli", return_value="fake"), patch.object(self.manager, "_validate"):
            for mode in ("disabled", "optimized", "disabled", "native", "optimized", "native"):
                with self.subTest(mode=mode), self.manager.change(mode):
                    pass
                data = tomllib.loads(self.config.read_text(encoding="utf8"))
                self.assertEqual(self.manager.status()["mode"], mode)
                self.assertEqual(self.manager.status()["enabled"], mode == "optimized")
                self.assertNotIn(VISION_PROVIDER, data.get("model_providers", {}))
                if mode == "disabled":
                    self.assertIs(data["mcp_servers"]["isolated_vision"]["enabled"], False)
                elif mode == "native":
                    self.assertEqual(data, native_config(original))

    def test_optimized_to_disabled_removes_the_managed_tool(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        with self.manager.change("disabled"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("mcp_servers", data)
        self.assertIs(data["tools"]["view_image"], False)
        with self.manager.change("native"):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), {"model": "test", "tools": {"view_image": True}})

    def test_failed_mode_transition_restores_disabled_configuration_and_state(self):
        with self.manager.change("disabled"):
            pass
        before, state_before = self.config.read_bytes(), self.manager.path.read_bytes()
        with patch("packet_inspector.image_config.find_codex_cli", return_value="fake"), patch.object(
                self.manager, "_validate", side_effect=ValueError("CLI rejected")), self.assertRaises(ValueError):
            with self.manager.change("optimized"):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.path.read_bytes(), state_before)
        self.assertEqual(self.manager.status()["mode"], "disabled")
        self.assertFalse(self.manager.profile_path.exists())

    def test_restoring_disabled_tools_keeps_unrelated_user_edits(self):
        with self.manager.change("disabled"):
            pass
        text = self.config.read_text(encoding="utf8").replace('command="original-reader"', 'command="updated-reader"')
        self.config.write_text(text, encoding="utf8")
        with self.manager.change("native"):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(data["mcp_servers"]["isolated_vision"]["command"], "updated-reader")
        self.assertNotIn("enabled", data["mcp_servers"]["isolated_vision"])

    def test_disabled_mode_conflict_does_not_overwrite_user_changes(self):
        with self.manager.change("disabled"):
            pass
        changed = self.config.read_text(encoding="utf8").replace("view_image = false", "view_image = true")
        self.config.write_text(changed, encoding="utf8")
        with self.assertRaises(ValueError), self.manager.change("native"):
            pass
        self.assertEqual(self.config.read_text(encoding="utf8"), changed)

    def test_missing_isolated_tool_installs_independent_stdio_tool(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("url", data["mcp_servers"]["isolated_vision"])
        self.assertTrue(Path(data["mcp_servers"]["isolated_vision"]["args"][-1]).is_file())
        self.assertTrue(self.manager.status()["managed_tool_installed"])
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), {"model": "test", "tools": {"view_image": True}})
        self.assertFalse(self.manager.status()["managed_tool_installed"])

    def test_failed_enable_rolls_back_both_files(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "settings failed"):
            with patch("packet_inspector.codex_runtime.shutil.which", return_value="fake"), patch.object(
                    self.manager, "_validate"), self.manager.change(True):
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.manager.path.exists())
        self.assertFalse(self.manager.profile_path.exists())
        self.assertFalse(self.manager.state)

    def test_cli_missing_does_not_change_config(self):
        before = self.config.read_bytes()
        with patch("packet_inspector.image_config.find_codex_cli", return_value=None), self.assertRaises(ValueError):
            with self.manager.change(True):
                pass
        self.assertEqual(self.config.read_bytes(), before)

    def test_disable_preserves_unrelated_changes_and_provider_rerouting(self):
        self.enable()
        import tomlkit
        data = tomlkit.parse(self.config.read_text(encoding="utf8"))
        data["mcp_servers"]["isolated_vision"]["command"] = "updated-existing-reader"
        data["model_providers"] = {"custom": {"name": "User provider", "base_url": "http://127.0.0.1:43210/relay/test/p/newroute"}}
        data["model_provider"] = "custom"
        data["unrelated_option"] = "keep"
        self.config.write_text(tomlkit.dumps(data), encoding="utf8")
        with self.manager.change(False):
            pass
        restored = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(restored["mcp_servers"]["isolated_vision"]["command"], "updated-existing-reader")
        self.assertEqual(restored["unrelated_option"], "keep")
        self.assertEqual(restored["model_provider"], "custom")
        self.assertEqual(restored["model_providers"], dict(data["model_providers"]))

    def test_conflicting_native_setting_is_not_overwritten(self):
        self.enable()
        modified = self.config.read_text(encoding="utf8").replace("view_image = false", "view_image = true")
        self.config.write_text(modified, encoding="utf8")
        with self.assertRaises(ValueError):
            with self.manager.change(False):
                pass
        self.assertEqual(self.config.read_text(encoding="utf8"), modified)

    def test_preexisting_native_disable_and_provider_auth_are_preserved(self):
        self.config.write_text('model_provider="corp"\n[tools]\nview_image=false\n'
            '[model_providers.corp]\nname="Corp"\nbase_url="https://gateway.invalid/v1"\n'
            'requires_openai_auth=false\nenv_key="TEST_PROVIDER_KEY"\nhttp_headers={"X-Company"="test"}\n', encoding="utf8")
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.enable()
        configured = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(configured["model_provider"], "corp")
        self.assertEqual(configured["model_providers"], original["model_providers"])
        self.assertNotIn("model_provider", tomllib.loads(self.manager.profile_path.read_text(encoding="utf8")))
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), native_config(original))

    def test_connection_owns_providers_independently_of_image_mode(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        providers = ProviderManager(self.manager.link, self.manager.relay_endpoint(),
                                    "https://chatgpt.com/backend-api/codex")
        routes = {}
        def sync():
            providers.sync(routes, lambda updated: routes.update(updated), retry_limit=10)
        def connection(path):
            data = tomllib.loads(path.read_text(encoding="utf8"))
            if "features" in data:
                data["features"].pop("view_image", None)
                if not data["features"]:
                    data.pop("features")
            return {key: value for key, value in data.items() if key not in {"tools", "mcp_servers"}}
        def effective_provider(data, base):
            selected = data.get("model_provider", base["model_provider"])
            self.assertEqual(selected, PROVIDER)
            return {**base["model_providers"][selected], **data.get("model_providers", {}).get(selected, {})}
        sync()
        paths = (self.config, self.manager.profile_path)
        connected = {path: connection(path) for path in paths}
        for data in connected.values():
            self.assertTrue(effective_provider(data, connected[self.config])["base_url"].startswith(providers.endpoint))
        with patch("packet_inspector.image_config.find_codex_cli", return_value="fake"), patch.object(self.manager, "_validate"):
            for mode in ("optimized", "native", "disabled", "optimized", "native"):
                with self.subTest(mode=mode), self.manager.change(mode):
                    pass
                self.assertEqual({path: connection(path) for path in paths}, connected)
            self.manager.link.restore()
            disconnected = {path: connection(path) for path in paths}
            for data in disconnected.values():
                self.assertEqual(effective_provider(data, disconnected[self.config])["base_url"],
                                 "https://chatgpt.com/backend-api/codex")
            with self.manager.change("optimized"):
                pass
            self.assertEqual({path: connection(path) for path in paths}, disconnected)
            with self.manager.change("native"):
                pass
            sync()  # Reconnection also updates profiles while image optimization is off.
            self.assertEqual({path: connection(path) for path in paths}, connected)

    def test_legacy_image_provider_normalization_does_not_block_refresh_or_uninstall(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        import tomlkit
        document = tomlkit.parse(self.config.read_text(encoding="utf8"))
        legacy = {"name": "Old image provider", "requires_openai_auth": True,
                  "base_url": "https://chatgpt.com/backend-api/codex"}
        document["model_providers"] = {VISION_PROVIDER: legacy}
        self.config.write_text(tomlkit.dumps(document), encoding="utf8")
        self.manager.profile_path.write_text(f'model_provider="{VISION_PROVIDER}"\n', encoding="utf8")
        state = deepcopy(self.manager.state)
        state.pop("profile_created", None)
        state.update(schema_version=4, managed_provider=legacy,
                     managed_profile={"model_provider": VISION_PROVIDER})
        state["originals"].append({"table": "model_providers", "name": VISION_PROVIDER,
                                   "present": False, "value": None})
        self.manager._save(state)
        providers = ProviderManager(self.manager.link, self.manager.relay_endpoint(),
                                    "https://chatgpt.com/backend-api/codex")
        providers.sync({}, lambda routes: None, retry_limit=10)
        profile = self.manager.profile_path.read_bytes()
        before = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(tomllib.loads(profile.decode())["model_provider"], PROVIDER)
        self.manager.installation_error = "old warning"
        with patch.object(self.manager, "_validate"), self.manager.change("optimized"):
            pass
        self.assertEqual(self.manager.status()["installation_error"], "")
        with self.manager.change("native"):
            pass
        after = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(after["model_providers"], before["model_providers"])
        self.assertEqual(after["model_provider"], before["model_provider"])
        self.assertEqual(self.manager.profile_path.read_bytes(), profile)

    def test_inline_vision_profile_is_preserved_in_place(self):
        with self.config.open("a", encoding="utf8") as out:
            out.write('[profiles.isolated-vision]\nmodel="test-vision-model"\nmodel_reasoning_effort="low"\nmodel_provider="custom"\n')
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.enable()
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8"))["profiles"], original["profiles"])
        self.assertFalse(self.manager.profile_path.exists())
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8"))["profiles"], original["profiles"])
        self.assertFalse(self.manager.profile_path.exists())

    def test_existing_native_profile_preserves_unrelated_edits_on_disable(self):
        self.manager.profile_path.write_text('model_provider="openai"\nmodel_reasoning_effort="low"\n', encoding="utf8")
        self.enable()
        with self.manager.profile_path.open("a", encoding="utf8") as out:
            out.write('model="different-vision-model"\n')
        with self.manager.change(False):
            pass
        restored = tomllib.loads(self.manager.profile_path.read_text(encoding="utf8"))
        self.assertEqual(restored["model_provider"], "openai")
        self.assertEqual(restored["model_reasoning_effort"], "low")
        self.assertEqual(restored["model"], "different-vision-model")

    def test_profile_write_failure_restores_root_configuration(self):
        before = self.config.read_bytes()
        original_write = ConfigLink._write
        def write(link, text):
            if link.config == self.manager.profile_path:
                raise OSError("profile disk full")
            return original_write(link, text)
        with patch.object(ConfigLink, "_write", new=write), patch(
                "packet_inspector.codex_runtime.shutil.which", return_value="fake"), self.assertRaises(OSError):
            with self.manager.change(True):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.manager.profile_path.exists())

    def test_cli_configuration_rejection_restores_both_layers(self):
        before = self.config.read_bytes()
        profile_before = b'model_reasoning_effort="low"\r\n'
        self.manager.profile_path.write_bytes(profile_before)
        with patch("packet_inspector.codex_runtime.shutil.which", return_value="fake"), patch.object(
                self.manager, "_validate", side_effect=ValueError("unsupported CLI")), self.assertRaises(ValueError):
            with self.manager.change(True):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.profile_path.read_bytes(), profile_before)

    def test_installed_cli_accepts_native_profile_and_disables_child_mcp_servers(self):
        cli = find_codex_cli()
        if not cli:
            self.skipTest("Codex CLI is not installed")
        with self.manager.change(True):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        profile = tomllib.loads(self.manager.profile_path.read_text(encoding="utf8"))
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.root)
        result = subprocess.run([cli, "--profile", "isolated-vision", "-c", "mcp_servers={" + ",".join(json.dumps(name) + "={enabled=false}" for name in sorted(set(data.get("mcp_servers", {})) | set(profile.get("mcp_servers", {})))) + "}",
            "mcp", "list", "--json"], env=environment, capture_output=True, text=True, encoding="utf8", errors="replace",
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "CLI rejected isolated profile or MCP override")
        servers = json.loads(result.stdout)
        self.assertTrue(servers)
        self.assertTrue(all(server["enabled"] is False for server in servers))


class ImageDirectoryStartupTests(unittest.TestCase):
    def test_old_settings_migrate_without_changing_the_selected_behavior(self):
        cases = [({}, "native"), ({"image_isolation_enabled": True}, "optimized"),
                 ({"image_isolation_enabled": False}, "native"),
                 ({"image_analysis_mode": "disabled"}, "disabled"),
                 ({"image_analysis_mode": "native", "image_isolation_enabled": True}, "native")]
        for saved, expected in cases:
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text('model="test"\n', encoding="utf8")
                data = root / "data"
                data.mkdir()
                (data / "settings.json").write_text(json.dumps(saved), encoding="utf8")
                with fixtures.InspectorServer(("127.0.0.1", 0), data, config, "http://127.0.0.1:9/v1") as server:
                    self.assertEqual(server.status()["image_analysis_mode"], expected)
                    self.assertEqual(server.settings["image_isolation_enabled"], expected == "optimized")

    def test_saved_image_directory_survives_server_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('model="test"\n', encoding="utf8")
            data_dir, custom = root / "data", root / "图片目录"
            data_dir.mkdir()
            (data_dir / "settings.json").write_text(json.dumps({"image_save_dir": str(custom)}), encoding="utf8")
            with fixtures.InspectorServer(("127.0.0.1", 0), data_dir, config, "http://127.0.0.1:9/v1") as server:
                self.assertEqual(server.image_isolation.directory, custom.resolve())
                self.assertEqual(server.status()["image_save_dir"], str(custom.resolve()))


class ImageRewriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.isolation = ImageIsolation(Path(self.temporary.name))

    def test_multiple_images_are_references_and_deduplicated(self):
        body = request_body()
        body["input"][-1]["content"].append(deepcopy(image_part()))
        wire, coding, metrics = self.isolation.prepare(encode(body), {}, "/responses", "off")
        converted = json.loads(decode_body(wire, coding))
        parts = converted["input"][-1]["content"]
        self.assertEqual(parts[0], body["input"][-1]["content"][0])
        self.assertEqual(converted["input"][:2], body["input"][:2])
        self.assertEqual([p["type"] for p in parts], ["input_text"] * 3)
        attachments = list(self.isolation.directory.glob("*.png"))
        self.assertEqual(len(attachments), 1)
        self.assertEqual(parts[1], {"type": "input_text", "text": str(attachments[0].resolve())})
        self.assertEqual(parts[2], parts[1])
        self.assertNotIn("data:image", wire.decode())
        self.assertEqual(metrics["image_isolation_removed"], 2)
        self.assertEqual(metrics["image_paths"], [str(attachments[0].resolve())])

    def test_rewrites_all_encodings_and_preserves_unrelated_original_bytes(self):
        raw = b' { "extra" : "\\u0061", "encrypted_content" : "EXACT\\nbytes", "input":' + encode(tool_image_body(custom=True)["input"]) + b' } '
        for coding, source in (("identity", raw), ("gzip", gzip.compress(raw)),
                               ("zstd", zstandard.ZstdCompressor().compress(raw))):
            for mode in ("off", "zstd"):
                with self.subTest(coding=coding, mode=mode):
                    wire, sent_coding, _ = self.isolation.prepare(source, {"Content-Encoding": coding}, "/responses", mode)
                    decoded = decode_body(wire, sent_coding)
                    self.assertTrue(decoded.startswith(b' { "extra" : "\\u0061", "encrypted_content" : "EXACT\\nbytes",'))
                    self.assertTrue(decoded.endswith(b' } '))
                    self.assertNotIn("input_image", decoded.decode())

    def test_tool_result_images_become_paths_without_changing_calls_or_other_content(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                body = tool_image_body(custom)
                original = deepcopy(body)
                wire, coding, metrics = self.isolation.prepare(encode(body), {}, "/responses", "off")
                result = json.loads(decode_body(wire, coding))
                reference = result["input"][2]["content"][1]
                expected = deepcopy(original)
                expected["input"][2]["content"][1] = reference
                expected["input"][-1]["output"][1] = reference
                self.assertEqual(result, expected)
                self.assertEqual(body, original)
                self.assertEqual(reference["type"], "input_text")
                self.assertTrue(Path(reference["text"]).is_file())
                self.assertEqual(metrics["image_isolation_removed"], 2)
                self.assertEqual(len(list(self.isolation.directory.glob("*.png"))), 1)

    def test_text_tool_results_remain_byte_transparent(self):
        for output in ("original text", [{"type": "input_text", "text": "original text"}]):
            raw = encode({"input": [{"type": "custom_tool_call_output", "call_id": "original", "output": output}]})
            self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))

    def test_plain_request_is_byte_transparent(self):
        raw = b' { "input" : [{"role":"user","content":"Just text"}] } '
        self.assertIsNone(self.isolation.prepare(raw, {}, "/responses", "off"))

    def test_only_native_catalog_entries_are_hidden_history_is_preserved(self):
        body = {"tools": [{"type": "function", "name": "view_image"}, {"type": "function", "name": "exec"}],
            "input": [{"type": "additional_tools", "tools": [{"type": "namespace", "name": "functions", "tools": [
                {"type": "function", "name": "view_image"}, {"type": "function", "name": "analyze_image"}]}]},
                {"type": "function_call", "name": "view_image", "call_id": "history", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "history", "output": "original text"}]}
        wire, coding, metrics = self.isolation.prepare(encode(body), {}, "/responses", "off")
        result = json.loads(decode_body(wire, coding))
        self.assertEqual([t["name"] for t in result["tools"]], ["exec"])
        self.assertEqual(result["input"][1:], body["input"][1:])
        self.assertEqual(metrics["native_image_tools_hidden"], 2)
        self.assertEqual(metrics["image_isolation_removed"], 0)

    def test_unsupported_images_fail_closed(self):
        invalid = [{"type": "input_image", "image_url": "https://invalid.example/image.png"},
                   {"type": "input_image", "file_id": "file-test"},
                   {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        for location in ("user", "function", "custom"):
            for part in invalid:
                body = request_body() if location == "user" else tool_image_body(location == "custom")
                body["input"][-1]["content" if location == "user" else "output"][1] = part
                with self.subTest(location=location, part=list(part)), self.assertRaises(ImageIsolationFailure):
                    self.isolation.prepare(encode(body), {}, "/responses", "off")

    def test_server_side_context_and_unknown_image_location_are_rejected(self):
        body = request_body()
        body["previous_response_id"] = "previous"
        with self.assertRaises(ImageIsolationFailure):
            self.isolation.prepare(encode(body), {}, "/responses", "off")
        with self.assertRaises(ImageIsolationFailure):
            self.isolation.prepare(encode({"input": [{"type": "unknown_result", "output": [image_part()]}]}), {}, "/responses", "off")
        with self.assertRaises(ImageIsolationFailure):
            self.isolation.prepare(encode({"input": [{"type": "custom_tool_call_output", "output": [
                {"type": "unknown_wrapper", "content": [image_part()]}]}]}), {}, "/responses", "off")

    def test_cache_budget_does_not_silently_restore_image(self):
        with patch("packet_inspector.image_isolation.MAX_ATTACHMENTS_BYTES", 1), self.assertRaises(ImageIsolationFailure) as caught:
            self.isolation.prepare(encode(request_body()), {}, "/responses", "off")
        self.assertEqual(caught.exception.code, "image_attachment_storage_full")

    def test_expiration_only_removes_owned_cache_files(self):
        self.isolation.directory.mkdir()
        owned = self.isolation.directory / ("a" * 64 + ".png")
        unrelated = self.isolation.directory / "keep.png"
        for path in (owned, unrelated):
            path.write_bytes(b"synthetic")
            os.utime(path, (1, 1))
        self.isolation.prepare(encode(request_body()), {}, "/responses", "off")
        self.assertFalse(owned.exists())
        self.assertTrue(unrelated.exists())


class ImageRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)
        cls.server.save_settings(dict(cls.server.settings))

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    api = fixtures.RelayTests.api
    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest

    def tearDown(self):
        self.server.settings["packet_size_protection_enabled"] = False
        restore = {}
        if self.server.settings["image_analysis_mode"] != "native":
            restore["image_analysis_mode"] = "native"
        if self.server.settings["image_save_dir"]:
            restore["image_save_dir"] = ""
        if restore:
            self.assertEqual(self.api("/api/settings", restore)[0], 200)

    def enable(self):
        with patch("packet_inspector.codex_runtime.shutil.which", return_value="fake-codex"), patch.object(self.server.image_config, "_validate"):
            status, result = self.api("/api/settings", {"image_isolation_enabled": True})
        self.assertEqual(status, 200)
        return result

    def test_modes_are_persisted_and_legacy_boolean_still_restores_native(self):
        with patch("packet_inspector.image_config.find_codex_cli", return_value="fake"), patch.object(self.server.image_config, "_validate"):
            for mode in ("disabled", "optimized", "disabled", "native"):
                status, result = self.api("/api/settings", {"image_analysis_mode": mode})
                self.assertEqual(status, 200, result)
                self.assertEqual(result["image_analysis_mode"], mode)
                self.assertEqual(result["image_isolation_enabled"], mode == "optimized")
                self.assertEqual(json.loads(self.server.settings_path.read_text())["image_analysis_mode"], mode)
                self.assertEqual(self.api("/health")[0], 200)
            self.assertEqual(self.api("/api/settings", {"image_analysis_mode": "disabled"})[0], 200)
            status, result = self.api("/api/settings", {"image_isolation_enabled": False})
            self.assertEqual((status, result["image_analysis_mode"]), (200, "native"))

    def test_invalid_modes_and_conflicts_do_not_modify_settings_or_config(self):
        before, config_before = self.server.settings_path.read_bytes(), self.config.read_bytes()
        for mode in (True, False, None, 1, [], {}, "off"):
            with self.subTest(mode=mode):
                self.assertEqual(self.api("/api/settings", {"image_analysis_mode": mode})[0], 400)
        self.assertEqual(self.api("/api/settings", {"image_analysis_mode": "disabled", "image_isolation_enabled": True})[0], 400)
        self.assertEqual(self.server.settings_path.read_bytes(), before)
        self.assertEqual(self.config.read_bytes(), config_before)

    def test_failed_save_rolls_back_disabled_mode(self):
        before = self.config.read_bytes()
        with patch.object(self.server, "save_settings", side_effect=OSError("settings disk full")):
            self.assertEqual(self.api("/api/settings", {"image_analysis_mode": "disabled"})[0], 500)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.server.settings["image_analysis_mode"], "native")
        self.assertEqual(self.server.image_config.status()["mode"], "native")

    def test_toggle_removes_native_tool_and_main_image_but_retains_inbound_evidence(self):
        raw = encode(request_body())
        self.assertEqual(self.relay(raw)[0], 200)
        self.assertEqual(self.up.requests[-1]["body"], raw)
        result = self.enable()
        self.assertTrue(result["image_isolation_enabled"])
        self.assertIs(tomllib.loads(self.config.read_text(encoding="utf8"))["tools"]["view_image"], False)
        self.assertEqual(self.relay(raw)[0], 200)
        outgoing = json.loads(self.up.requests[-1]["body"])
        self.assertEqual(outgoing["input"][-1]["content"][1]["type"], "input_text")
        meta, inbound = self.latest()
        self.assertEqual(meta["image_isolation_removed"], 1)
        self.assertEqual(inbound["body"]["input"][-1]["content"][1]["type"], "input_image")

    def test_tool_images_are_rewritten_before_forwarding_and_in_later_history(self):
        self.enable()
        for custom in (False, True):
            body = tool_image_body(custom)
            for followup in (False, True):
                if followup:
                    body["input"].append({"role": "user", "content": "Continue using the earlier tool result."})
                with self.subTest(custom=custom, followup=followup):
                    self.assertEqual(self.relay(encode(body))[0], 200)
                    outgoing = self.up.requests[-1]
                    coding = next((v for k, v in outgoing["headers"].items() if k.lower() == "content-encoding"), "identity")
                    decoded = decode_body(outgoing["body"], coding)
                    result = json.loads(decoded)
                    self.assertNotIn("input_image", decoded.decode())
                    self.assertEqual(result["input"][3], body["input"][3])
                    self.assertEqual(result["input"][4]["call_id"], "image-call")
                    self.assertEqual(result["input"][4]["output"][0], body["input"][4]["output"][0])
                    self.assertEqual(result["input"][4]["output"][1], result["input"][2]["content"][1])
                    meta, inbound = self.latest()
                    self.assertEqual(meta["image_isolation_removed"], 2)
                    self.assertEqual(inbound["body"]["input"][4]["output"][1]["type"], "input_image")

    def test_isolated_worker_keeps_image_and_private_header_is_removed(self):
        self.enable()
        raw = encode(request_body())
        self.server.image_config.state["worker_token"] = "legacy-test-worker"
        self.addCleanup(self.server.image_config.state.pop, "worker_token", None)
        self.assertEqual(self.relay(raw, headers={WORKER_HEADER: "legacy-test-worker"})[0], 200)
        self.assertEqual(self.up.requests[-1]["body"], raw)
        self.assertFalse(any(k.lower() == WORKER_HEADER.lower() for k in self.up.requests[-1]["headers"]))
        self.assertTrue(self.latest()[0]["image_isolation_worker"])
        self.server.image_config.state.pop("worker_token", None)
        self.assertEqual(self.relay(raw, headers={WORKER_HEADER: "untrusted"})[0], 200)
        self.assertNotIn("input_image", self.up.requests[-1]["body"].decode())

    def test_independent_analysis_keeps_images_without_a_special_provider_or_header(self):
        self.enable()
        body = request_body()
        body["input"][-1]["content"][0]["text"] = IMAGE_ANALYST_PREFIXES[-1] + " Read the error."
        raw = encode(body)
        self.server.settings["packet_size_protection_enabled"] = True
        capture_before = self.server.settings["capture_enabled"]
        try:
            for capture in (True, False):
                self.server.settings["capture_enabled"] = capture
                for coding in ("identity", "zstd"):
                    wire = zstandard.ZstdCompressor().compress(raw) if coding == "zstd" else raw
                    with self.subTest(capture=capture, coding=coding), patch.object(
                            self.server.image_isolation, "prepare", side_effect=AssertionError("independent image was rewritten")), patch.object(
                            self.server.overflow, "prepare", side_effect=AssertionError("independent image was compacted")):
                        self.assertEqual(self.relay(wire, headers={"Content-Encoding": coding})[0], 200)
                    outgoing = self.up.requests[-1]
                    encoding = next((value for key, value in outgoing["headers"].items()
                                     if key.lower() == "content-encoding"), "identity")
                    self.assertEqual(decode_body(outgoing["body"], encoding), raw)
                    if capture:
                        self.assertTrue(self.latest()[0]["image_isolation_worker"])
        finally:
            self.server.settings["capture_enabled"] = capture_before

    def test_quoted_analyst_prompt_in_a_main_request_does_not_bypass_image_replacement(self):
        self.enable()
        body = request_body()
        body["input"][-1]["content"][0]["text"] = "Explain this prompt: " + IMAGE_ANALYST_PREFIXES[-1]
        self.assertEqual(self.relay(encode(body))[0], 200)
        self.assertNotIn("input_image", self.up.requests[-1]["body"].decode())

    def test_invalid_image_never_reaches_upstream(self):
        self.enable()
        count = len(self.up.requests)
        raw = encode({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://invalid.example/a.png"}]}]})
        self.assertEqual(self.relay(raw)[0], 422)
        self.assertEqual(len(self.up.requests), count)

    def test_overflow_path_receives_only_the_rewritten_request(self):
        self.enable()
        self.server.settings["packet_size_protection_enabled"] = True
        def compact(original, encoding, source, headers, *args, **kwargs):
            coding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "identity")
            decoded = decode_body(source, coding)
            self.assertNotIn("input_image", decoded.decode())
            self.assertNotIn("data:image", decoded.decode())
            return original, encoding, {}
        with patch.object(self.server.overflow, "prepare", side_effect=compact) as prepare:
            self.assertEqual(self.relay(encode(request_body()))[0], 200)
            prepare.assert_called_once()

    def test_failed_settings_write_restores_config_and_state(self):
        config_before = self.config.read_bytes()
        with patch("packet_inspector.codex_runtime.shutil.which", return_value="fake"), patch.object(self.server.image_config, "_validate"), patch.object(
                self.server, "save_settings", side_effect=OSError("disk full")):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True})[0], 500)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse(self.server.settings["image_isolation_enabled"])
        self.assertFalse(self.server.image_config.state.get("enabled"))

    def test_combined_settings_failure_restores_native_and_mcp_config(self):
        config_before, settings_before = self.config.read_bytes(), self.server.settings_path.read_bytes()
        with patch("packet_inspector.codex_runtime.shutil.which", return_value="fake"), patch.object(self.server.image_config, "_validate"), patch(
                "packet_inspector.server.configure_startup", side_effect=ValueError("startup failed")):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True, "start_on_login": True})[0], 400)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.server.settings_path.read_bytes(), settings_before)

    def test_boolean_validation(self):
        for value in (1, "true", None):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": value})[0], 400)

    def test_image_save_directory_default_and_custom_path_preserve_old_images(self):
        default = (self.server.data_dir / "image-attachments").resolve()
        status = self.api("/api/status")[1]
        self.assertTrue(status["image_save_dir_supported"])
        self.assertEqual(status["image_save_dir"], str(default))
        self.assertEqual(status["image_save_dir_default"], str(default))
        self.enable()
        self.assertEqual(self.relay(encode(request_body()))[0], 200)
        old_path = Path(json.loads(self.up.requests[-1]["body"])["input"][-1]["content"][-1]["text"])
        custom = Path(self.temp.name) / "图片目录 with spaces" / "attachments"
        code, status = self.api("/api/settings", {"image_save_dir": str(custom)})
        self.assertEqual(code, 200, status)
        self.assertTrue(custom.is_dir())
        self.assertEqual(status["image_save_dir"], str(custom.resolve()))
        saved = json.loads(self.server.settings_path.read_text(encoding="utf8"))
        self.assertEqual(saved["image_save_dir"], str(custom.resolve()))
        self.assertEqual(self.relay(encode(request_body()))[0], 200)
        new_path = Path(json.loads(self.up.requests[-1]["body"])["input"][-1]["content"][-1]["text"])
        self.assertEqual(new_path.parent, custom.resolve())
        self.assertTrue(new_path.is_file())
        self.assertTrue(old_path.is_file())
        code, status = self.api("/api/settings", {"image_save_dir": ""})
        self.assertEqual(code, 200)
        self.assertEqual(status["image_save_dir"], str(default))
        self.assertEqual(self.server.settings["image_save_dir"], "")
        self.assertTrue(new_path.is_file())

    def test_invalid_image_save_directory_preserves_settings_and_native_tool(self):
        existing_file = Path(self.temp.name) / "not-a-directory.txt"
        existing_file.write_text("keep", encoding="utf8")
        before = self.server.settings_path.read_bytes()
        config_before = self.config.read_bytes()
        original = self.server.image_isolation
        for value in (None, 17, True, "relative-images", "bad\x00path", str(existing_file)):
            with self.subTest(value=value):
                code, _ = self.api("/api/settings", {"image_save_dir": value, "image_isolation_enabled": True})
                self.assertEqual(code, 400)
                self.assertEqual(self.server.settings_path.read_bytes(), before)
                self.assertEqual(self.config.read_bytes(), config_before)
                self.assertIs(self.server.image_isolation, original)
        self.assertEqual(existing_file.read_text(encoding="utf8"), "keep")

    def test_image_directory_permission_or_settings_failure_keeps_active_directory(self):
        original = self.server.image_isolation
        before = self.server.settings_path.read_bytes()
        custom = Path(self.temp.name) / "new-image-folder"
        with patch("packet_inspector.image_isolation.tempfile.TemporaryFile", side_effect=PermissionError):
            self.assertEqual(self.api("/api/settings", {"image_save_dir": str(custom)})[0], 400)
        with patch.object(self.server, "save_settings", side_effect=OSError("disk full")):
            self.assertEqual(self.api("/api/settings", {"image_save_dir": str(custom)})[0], 500)
        self.assertIs(self.server.image_isolation, original)
        self.assertEqual(self.server.settings_path.read_bytes(), before)

    def test_new_user_installs_stdio_tool_and_uninstall_restores_native(self):
        if not find_codex_cli():
            self.skipTest("Codex CLI is not installed")
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        model_requests = len(self.up.requests)
        code, status = self.api("/api/settings", {"image_isolation_enabled": True})
        self.assertEqual(code, 200, status)
        self.assertTrue(status["image_tool"]["managed_tool_installed"])
        self.assertEqual(status["image_tool"]["transport"], "stdio")
        registration = tomllib.loads(self.config.read_text(encoding="utf8"))["mcp_servers"]["isolated_vision"]
        self.assertNotIn("url", registration)
        self.assertTrue(Path(registration["args"][-1]).is_file())
        self.assertFalse(hasattr(self.server, "image_tool"))
        self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True})[0], 200)
        self.assertEqual(len(self.up.requests), model_requests)
        self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": False})[0], 200)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), native_config(original))
        self.assertFalse(Path(registration["args"][-1]).exists())
        self.assertFalse(self.server.image_config.status()["managed_tool_installed"])

    def test_http_image_tool_endpoint_is_retired(self):
        code, result = self.api("/mcp/image/retired", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(code, 404)
        self.assertEqual(result["error"], "image_tool_uses_stdio")


if __name__ == "__main__":
    unittest.main()
