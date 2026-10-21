"""Synthetic-only verification of native-tool replacement and image isolation."""
import base64
from copy import deepcopy
import gzip
import http.client
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_packet_inspector as fixtures
import zstandard

from packet_inspector.analyzer import decode_body, encode
from packet_inspector.config_link import ConfigLink
from packet_inspector.image_config import ImageToolConfig, VISION_PROVIDER, WORKER_HEADER
from packet_inspector.image_isolation import ImageIsolation, ImageIsolationFailure
from packet_inspector.image_tool import ImageToolRPC


def image_part():
    # This is a synthetic signature followed by dummy bytes, not a user image.
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-data" * 12
    return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(payload).decode()}


def request_body():
    return {"model": "test", "input": [
        {"role": "developer", "content": "Keep all original instructions."},
        {"type": "compaction", "encrypted_content": "unchanged-encrypted-state"},
        {"role": "user", "content": [{"type": "input_text", "text": "Read the error in this image."}, image_part()]}]}


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
        with patch("packet_inspector.image_config.shutil.which", return_value="fake-codex"), self.manager.change(True):
            pass

    def test_native_tool_disabled_existing_mcp_preserved_and_restored(self):
        original = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.enable()
        configured = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertIs(configured["tools"]["view_image"], False)
        self.assertEqual(configured["mcp_servers"], original["mcp_servers"])
        self.assertEqual(configured["profiles"]["isolated-vision"]["model_provider"], VISION_PROVIDER)
        self.assertTrue(self.manager.is_worker(configured["model_providers"][VISION_PROVIDER]["http_headers"][WORKER_HEADER]))
        self.assertTrue(Path(self.manager.status()["backup"]).is_file())
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), original)

    def test_missing_isolated_tool_gets_text_only_http_fallback(self):
        self.config.write_text('model="test"\n', encoding="utf8")
        self.enable()
        data = tomllib.loads(self.config.read_text(encoding="utf8"))
        self.assertTrue(data["mcp_servers"]["isolated_vision"]["url"].startswith("http://127.0.0.1:43210/mcp/image/"))
        with self.manager.change(False):
            pass
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf8")), {"model": "test"})

    def test_failed_enable_rolls_back_both_files(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "settings failed"):
            with patch("packet_inspector.image_config.shutil.which", return_value="fake"), self.manager.change(True):
                raise RuntimeError("settings failed")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.manager.path.exists())
        self.assertFalse(self.manager.state)

    def test_cli_missing_does_not_change_config(self):
        before = self.config.read_bytes()
        with patch("packet_inspector.image_config.shutil.which", return_value=None), self.assertRaises(ValueError):
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
        self.assertIn("isolated_vision.analyze_image", parts[1]["text"])
        self.assertNotIn("data:image", wire.decode())
        self.assertEqual(metrics["image_isolation_removed"], 2)
        self.assertEqual(len(list(self.isolation.directory.glob("*.png"))), 1)

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
        if self.server.settings["image_isolation_enabled"]:
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": False})[0], 200)

    def enable(self):
        with patch("packet_inspector.image_config.shutil.which", return_value="fake-codex"):
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

    def test_combined_settings_failure_restores_native_and_mcp_config(self):
        config_before, settings_before = self.config.read_bytes(), self.server.settings_path.read_bytes()
        with patch("packet_inspector.image_config.shutil.which", return_value="fake"), patch(
                "packet_inspector.server.configure_startup", side_effect=ValueError("startup failed")):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": True, "start_on_login": True})[0], 400)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.server.settings_path.read_bytes(), settings_before)

    def test_boolean_validation(self):
        for value in (1, "true", None):
            self.assertEqual(self.api("/api/settings", {"image_isolation_enabled": value})[0], 400)

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
        with patch("packet_inspector.image_tool.subprocess.run", side_effect=run):
            answer = self.server.image_tool.analyze({"path": str(image), "instruction": "Read the error"})
        self.assertEqual(answer, "Parsed error message")
        command, options = observed[0]
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--image") + 1], str(image.resolve()))
        self.assertIn('mcp_servers."isolated_vision".enabled=false', command)
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.config.parent))


if __name__ == "__main__":
    unittest.main()
