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
from types import SimpleNamespace

from tests import test_packet_inspector as fixtures
import zstandard

from packet_inspector.analyzer import decode_body, encode
from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig, VISION_PROVIDER, WORKER_HEADER, find_codex_cli
from packet_inspector.image_isolation import ImageIsolation, ImageIsolationFailure
from packet_inspector.image_tool import ImageToolRPC, disabled_mcp_override


def image_part():
    # This is a synthetic signature followed by dummy bytes, not a user image.
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-data" * 12
    return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(payload).decode()}


def request_body():
    return {"model": "test", "input": [
        {"role": "developer", "content": "Keep all original instructions."},
        {"type": "compaction", "encrypted_content": "unchanged-encrypted-state"},
        {"role": "user", "content": [{"type": "input_text", "text": "Read the error in this image."}, image_part()]}]}


class ImageCliDiscoveryTests(unittest.TestCase):
    def test_configured_cli_takes_priority(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": "environment-cli"}), patch(
                "packet_inspector.image_config.shutil.which") as which:
            self.assertEqual(find_codex_cli("configured-cli"), "configured-cli")
        which.assert_not_called()

    def test_desktop_only_user_does_not_need_cli_on_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "OpenAI" / "Codex" / "bin"
            older, newer = cache / "older" / "codex.exe", cache / "newer" / "codex.exe"
            for path in (older, newer):
                path.parent.mkdir(parents=True)
                path.write_text("synthetic CLI placeholder", encoding="ascii")
            os.utime(older, ns=(1, 1))
            os.utime(newer, ns=(2, 2))
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary, "CODEX_CLI_PATH": ""}), patch(
                    "packet_inspector.image_config.shutil.which", return_value=None):
                self.assertEqual(find_codex_cli(), str(newer.resolve()))
                newer.unlink()
                self.assertEqual(find_codex_cli(), str(older.resolve()))

    def test_missing_desktop_and_cli_return_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"LOCALAPPDATA": temporary, "CODEX_CLI_PATH": ""}), patch(
                "packet_inspector.image_config.shutil.which", return_value=None):
            self.assertIsNone(find_codex_cli())


class ImageConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model="test"\n[tools]\nweb_search=true\n'
            '[mcp_servers.isolated_vision]\ncommand="original-reader"\nargs=["existing.py"]\n'
            '[mcp_servers.other]\ncommand="unrelated"\n', encoding="utf8")
        self.manager = ImageToolConfig(ConfigLink(self.config, self.root / "link.json"), self.root,
            lambda: "http://127.0.0.1:43210", lambda: "http://127.0.0.1:43210/relay/test")

    def enable(self):
        with patch("packet_inspector.image_config.shutil.which", return_value="fake-codex"), patch.object(
                self.manager, "_validate"), self.manager.change(True):
            pass

    def test_native_tool_disabled_existing_mcp_preserved_and_restored(self):
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.enable()
        configured = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertIs(configured["tools"]["view_image"], False)
        self.assertEqual(configured["mcp_servers"], original["mcp_servers"])
        self.assertEqual(tomllib.loads(self.manager.profile_path.read_text(encoding="utf8"))["model_provider"], VISION_PROVIDER)
        self.assertNotIn("isolated-vision", configured.get("profiles", {}))
        self.assertTrue(self.manager.is_worker(configured["model_providers"][VISION_PROVIDER]["http_headers"][WORKER_HEADER]))
        self.assertTrue(Path(self.manager.status()["backup"]).is_file())
        self.assertFalse(self.manager.status()["managed_tool_installed"])
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), original)

    def test_missing_isolated_tool_gets_text_only_http_fallback(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertTrue(data["mcp_servers"]["isolated_vision"]["url"].startswith("http://127.0.0.1:43210/mcp/image/"))
        self.assertTrue(self.manager.status()["managed_tool_installed"])
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), {"model": "test"})
        self.assertFalse(self.manager.status()["managed_tool_installed"])

    def test_failed_enable_rolls_back_both_files(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "settings failed"):
            with patch("packet_inspector.image_config.shutil.which", return_value="fake"), patch.object(
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
        data["model_providers"][VISION_PROVIDER]["base_url"] = "http://127.0.0.1:43210/relay/test/p/newroute"
        data["unrelated_option"] = "keep"
        self.config.write_text(tomlkit.dumps(data), encoding="utf8")
        with self.manager.change(False):
            pass
        restored = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertEqual(restored["mcp_servers"]["isolated_vision"]["command"], "updated-existing-reader")
        self.assertEqual(restored["unrelated_option"], "keep")

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
        worker = configured["model_providers"][VISION_PROVIDER]
        self.assertEqual(worker["env_key"], "TEST_PROVIDER_KEY")
        self.assertIs(worker["requires_openai_auth"], False)
        self.assertEqual(worker["http_headers"]["X-Company"], "test")
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), original)

    def test_legacy_vision_profile_migrates_and_keeps_its_model_preferences(self):
        with self.config.open("a", encoding="utf8") as out:
            out.write('[profiles.isolated-vision]\nmodel="test-vision-model"\nmodel_reasoning_effort="low"\n')
        self.enable()
        profile = tomllib.loads(self.manager.profile_path.read_text(encoding="utf8"))
        self.assertEqual(profile["model"], "test-vision-model")
        self.assertEqual(profile["model_reasoning_effort"], "low")
        with self.manager.change(False):
            pass
        self.assertNotIn("model_provider", tomllib.loads(self.config.read_text(encoding="utf8"))["profiles"]["isolated-vision"])
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
                "packet_inspector.image_config.shutil.which", return_value="fake"), self.assertRaises(OSError):
            with self.manager.change(True):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.manager.profile_path.exists())

    def test_cli_configuration_rejection_restores_both_layers(self):
        before = self.config.read_bytes()
        profile_before = b'model_reasoning_effort="low"\r\n'
        self.manager.profile_path.write_bytes(profile_before)
        with patch("packet_inspector.image_config.shutil.which", return_value="fake"), patch.object(
                self.manager, "_validate", side_effect=ValueError("unsupported CLI")), self.assertRaises(ValueError):
            with self.manager.change(True):
                pass
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.manager.profile_path.read_bytes(), profile_before)

    def test_installed_cli_accepts_native_profile_and_disables_child_mcp_servers(self):
        cli = shutil.which("codex")
        if not cli:
            self.skipTest("Codex CLI is not installed")
        with self.manager.change(True):
            pass
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        profile = tomllib.loads(self.manager.profile_path.read_text(encoding="utf8"))
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.root)
        result = subprocess.run([cli, "--profile", "isolated-vision", "-c", disabled_mcp_override(data, profile),
            "mcp", "list", "--json"], env=environment, capture_output=True, text=True, encoding="utf8", errors="replace",
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "CLI rejected isolated profile or MCP override")
        servers = json.loads(result.stdout)
        self.assertTrue(servers)
        self.assertTrue(all(server["enabled"] is False for server in servers))


class ImageDirectoryStartupTests(unittest.TestCase):
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
        raw = b' { "extra" : "\\u0061", "encrypted_content" : "EXACT\\nbytes", "input":' + encode(request_body()["input"]) + b' } '
        for coding, source in (("identity", raw), ("gzip", gzip.compress(raw)),
                               ("zstd", zstandard.ZstdCompressor().compress(raw))):
            for mode in ("off", "zstd"):
                with self.subTest(coding=coding, mode=mode):
                    wire, sent_coding, _ = self.isolation.prepare(source, {"Content-Encoding": coding}, "/responses", mode)
                    decoded = decode_body(wire, sent_coding)
                    self.assertTrue(decoded.startswith(b' { "extra" : "\\u0061", "encrypted_content" : "EXACT\\nbytes",'))
                    self.assertTrue(decoded.endswith(b' } '))
                    self.assertNotIn("input_image", decoded.decode())

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
        for part in invalid:
            body = request_body()
            body["input"][-1]["content"][1] = part
            with self.subTest(part=list(part)), self.assertRaises(ImageIsolationFailure):
                self.isolation.prepare(encode(body), {}, "/responses", "off")

    def test_server_side_context_and_unknown_image_location_are_rejected(self):
        body = request_body()
        body["previous_response_id"] = "previous"
        with self.assertRaises(ImageIsolationFailure):
            self.isolation.prepare(encode(body), {}, "/responses", "off")
        with self.assertRaises(ImageIsolationFailure):
            self.isolation.prepare(encode({"input": [{"type": "function_call_output", "output": [image_part()]}]}), {}, "/responses", "off")

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
        if self.server.settings["image_isolation_enabled"]:
            restore["image_isolation_enabled"] = False
        if self.server.settings["image_save_dir"]:
            restore["image_save_dir"] = ""
        if restore:
            self.assertEqual(self.api("/api/settings", restore)[0], 200)

    def enable(self):
        with patch("packet_inspector.image_config.shutil.which", return_value="fake-codex"), patch.object(self.server.image_config, "_validate"):
            status, result = self.api("/api/settings", {"image_isolation_enabled": True})
        self.assertEqual(status, 200)
        return result

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

    def test_isolated_worker_keeps_image_and_private_header_is_removed(self):
        self.enable()
        raw = encode(request_body())
        self.assertEqual(self.relay(raw, headers={WORKER_HEADER: self.server.image_config.state["worker_token"]})[0], 200)
        self.assertEqual(self.up.requests[-1]["body"], raw)
        self.assertFalse(any(k.lower() == WORKER_HEADER.lower() for k in self.up.requests[-1]["headers"]))
        self.assertTrue(self.latest()[0]["image_isolation_worker"])
        self.assertEqual(self.relay(raw, headers={WORKER_HEADER: "untrusted"})[0], 200)
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
        with patch("packet_inspector.image_config.shutil.which", return_value="fake"), patch.object(self.server.image_config, "_validate"), patch.object(
                self.server, "save_settings", side_effect=OSError("disk full")):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True})[0], 500)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse(self.server.settings["image_isolation_enabled"])
        self.assertFalse(self.server.image_config.state.get("enabled"))

    def test_combined_settings_failure_restores_native_and_mcp_config(self):
        config_before, settings_before = self.config.read_bytes(), self.server.settings_path.read_bytes()
        with patch("packet_inspector.image_config.shutil.which", return_value="fake"), patch.object(self.server.image_config, "_validate"), patch(
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

    def mcp(self, message, session=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Mcp-Session-Id"] = session
        connection.request(method, self.server.image_config.mcp_path(), encode(message) if message is not None else None, headers)
        response = connection.getresponse()
        result = response.read()
        value = (response.status, json.loads(result) if result else None, response.getheader("Mcp-Session-Id"))
        connection.close()
        return value

    def test_new_user_installs_bundled_tool_and_cli_loads_registration(self):
        if not find_codex_cli():
            self.skipTest("Codex CLI is not installed")
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertNotIn("isolated_vision", original.get("mcp_servers", {}))
        model_requests = len(self.up.requests)
        code, status = self.api("/api/settings", {"image_isolation_enabled": True})
        self.assertEqual(code, 200, status)
        self.assertTrue(status["image_tool"]["managed_tool_installed"])
        installed = tomllib.loads(self.config.read_text(encoding="utf8"))
        registration = installed["mcp_servers"]["isolated_vision"]
        self.assertTrue(registration["url"].endswith(self.server.image_config.mcp_path()))
        self.assertNotIn("command", registration)
        self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True})[0], 200)
        reloaded = ImageToolConfig(self.server.link, self.server.data_dir,
            self.server.image_config.api_base, self.server.image_config.relay_endpoint)
        self.assertTrue(reloaded.status()["managed_tool_installed"])
        _, _, session = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertTrue(session)
        code, listing, _ = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        self.assertEqual(code, 200)
        self.assertEqual([tool["name"] for tool in listing["result"]["tools"]], ["analyze_image"])
        self.assertEqual(len(self.up.requests), model_requests)
        self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": False})[0], 200)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), original)
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session)[0], 404)

    def test_fallback_mcp_returns_only_text_and_enforces_call_limit(self):
        self.enable()
        code, initialized, session = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(code, 200)
        self.assertTrue(session)
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)[0], 202)
        listed = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)[1]
        self.assertEqual(listed["result"]["tools"][0]["name"], "analyze_image")
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "analyze_image", "arguments": {"path": "dummy"}}}
        with patch.object(self.server.image_tool, "analyze", return_value="Visible error text") as analyze:
            for _ in range(24):
                result = self.mcp(call, session)[1]["result"]
                self.assertEqual(result["content"], [{"type": "text", "text": "Visible error text"}])
            self.assertTrue(self.mcp(call, session)[1]["result"]["isError"])
            self.assertEqual(analyze.call_count, 24)
        self.assertEqual(self.mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, "bad-session")[0], 404)
        self.assertEqual(self.mcp(None, session, "GET")[0], 405)
        self.assertEqual(self.mcp(None, session, "DELETE")[0], 204)

    def test_fallback_runs_a_fresh_cli_with_path_and_returns_text(self):
        self.enable()
        image = Path(self.temp.name) / "synthetic.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        observed = []
        def run(command, **kwargs):
            observed.append((command, kwargs))
            Path(command[command.index("--output-last-message") + 1]).write_text("Parsed error message", encoding="utf8")
            return SimpleNamespace(returncode=0)
        with patch("packet_inspector.image_tool.find_codex_cli", return_value="fake-codex"), patch(
                "packet_inspector.image_tool.subprocess.run", side_effect=run):
            answer = self.server.image_tool.analyze({"path": str(image), "instruction": "Read the error"})
        self.assertEqual(answer, "Parsed error message")
        command, options = observed[0]
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--image") + 1], str(image.resolve()))
        override = command[command.index("-c") + 1]
        self.assertIn('"isolated_vision"={enabled=false}', override)
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.config.parent))

    def test_bundled_tool_rediscovers_cli_after_desktop_update(self):
        self.enable()
        self.server.image_config.state["cli"] = str(Path(self.temp.name) / "removed-cli.exe")
        image = Path(self.temp.name) / "synthetic.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        def run(command, **kwargs):
            self.assertEqual(command[0], "updated-codex")
            Path(command[command.index("--output-last-message") + 1]).write_text("Image text", encoding="utf8")
            return SimpleNamespace(returncode=0)
        with patch("packet_inspector.image_tool.find_codex_cli", return_value="updated-codex"), patch(
                "packet_inspector.image_tool.subprocess.run", side_effect=run):
            self.assertEqual(self.server.image_tool.analyze({"path": str(image)}), "Image text")

    def test_image_analysis_is_counted_while_running_and_respects_upgrade_drain(self):
        self.enable()
        _, _, session = self.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "analyze_image", "arguments": {"path": "dummy"}}}
        def analyze(arguments):
            self.assertGreaterEqual(self.server.active_requests, 1)
            return "Visible text"
        with patch.object(self.server.image_tool, "analyze", side_effect=analyze):
            self.assertEqual(self.mcp(call, session)[0], 200)
        self.assertEqual(self.server.active_requests, 0)
        self.server.upgrade_draining = True
        try:
            with patch.object(self.server.image_tool, "analyze") as analyze:
                self.assertEqual(self.mcp(call, session)[0], 503)
                analyze.assert_not_called()
        finally:
            self.server.upgrade_draining = False
        self.assertEqual(self.server.active_requests, 0)


if __name__ == "__main__":
    unittest.main()
