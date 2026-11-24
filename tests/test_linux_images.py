"""Portable tests for remote image upload ordering and tool configuration."""
import base64
from contextlib import chdir
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from packet_inspector.analyzer import encode
from packet_inspector.isolated_image_tool import ImageTool, analyze
from packet_inspector.linux_image_control import linux_image_runtime
from packet_inspector.linux_images import LinuxImageTarget, LinuxImageTransfer, linux_image_edits


DIRECTORY = "/home/test/.cache/codex-request-optimizer/images"
TEST_ATTACHMENT = b"\x89PNG\r\n\x1a\nsynthetic-picture"
ATTACHMENT = "data:image/png;base64," + base64.b64encode(TEST_ATTACHMENT).decode()
NAME = "i-" + hashlib.sha256(TEST_ATTACHMENT).hexdigest()[:30] + ".png"
NATIVE = {"type": "function", "name": "view_image"}


def image_input():
    return {"type": "input_image", "image_url": ATTACHMENT}


class ImageTransferTests(unittest.TestCase):
    def setUp(self):
        self.transfer = LinuxImageTransfer()
        self.stored = {}
        self.calls = []
        self.target = LinuxImageTarget(DIRECTORY, self.send)
        self.request = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": r'<image name="fixture" path="C:\Temp\fixture.png"></image>'},
            image_input(),
        ]}], "extra": {"mode": "preserve"}}

    def send(self, action, data_dir, entries, referenced):
        self.assertEqual(action, "image_store")
        self.assertEqual(data_dir, DIRECTORY.removesuffix("/images"))
        self.calls.append({key: "data" in entry for key, entry in entries.items()})
        for key, entry in entries.items():
            if "data" in entry:
                data = base64.b64decode(entry["data"], validate=True)
                self.assertEqual(len(data), entry["size"])
                self.stored[key] = data
        return {"directory": DIRECTORY, "confirmed": [key for key in entries if key in self.stored]}

    def rewrite(self):
        return self.transfer.rewrite(encode(self.request), {}, self.target, "device/account")

    def test_replaces_paths_only_after_confirming_bytes_and_reuploads_if_missing(self):
        changed, metadata = self.rewrite()
        self.assertEqual(self.stored, {NAME: TEST_ATTACHMENT})
        path = DIRECTORY + "/" + NAME
        parts = json.loads(changed)["input"][0]["content"]
        self.assertIn(path, parts[0]["text"])
        self.assertEqual(parts[1], {"type": "input_text", "text": path})
        self.assertEqual(json.loads(changed)["extra"], self.request["extra"])
        self.assertEqual(metadata["image_paths"], [path])
        self.rewrite()
        self.assertEqual(self.calls, [{NAME: True}, {NAME: False}])
        self.stored.clear()
        self.rewrite()
        self.assertEqual(self.calls[-2:], [{NAME: False}, {NAME: True}])
        self.assertEqual(self.stored, {NAME: TEST_ATTACHMENT})

    def test_old_native_tool_and_tool_results_are_preserved(self):
        self.request["tools"] = [{"type": "namespace", "name": "functions", "tools": [NATIVE]}]
        self.assertIsNone(self.rewrite())
        self.assertEqual(self.calls, [])
        del self.request["tools"]
        self.request["input"] += [{"type": "function_call", "name": "view_image", "call_id": "native"},
                                   {"type": "function_call_output", "call_id": "native", "output": [image_input()]},
                                   {"type": "function_call_output", "call_id": "exec", "output": [image_input()]}]
        changed, metadata = self.rewrite()
        self.assertEqual(metadata["image_isolation_removed"], 2)
        items = json.loads(changed)["input"]
        self.assertEqual(items[-2]["output"], [image_input()])
        self.assertEqual(items[-1]["output"][0]["type"], "input_text")

    def test_rejects_acknowledgement_from_a_different_directory(self):
        self.target = LinuxImageTarget(DIRECTORY, lambda *args, **kwargs: {
            "directory": "/tmp/other/images", "confirmed": [NAME]})
        with self.assertRaisesRegex(ValueError, "未确认"):
            self.rewrite()


class LinuxToolTests(unittest.TestCase):
    def test_installer_reports_actual_script_changes_with_the_same_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            tool_dir = home / "tools" / "codex-inspector-image-tool"
            tool_dir.mkdir(parents=True)
            with patch.dict(os.environ, {"CODEX_CLI_PATH": sys.executable}), \
                 patch("packet_inspector.linux_image_control.linux_private_directory"), \
                 patch("packet_inspector.linux_image_control.shutil.which", return_value=sys.executable), \
                 patch("packet_inspector.linux_image_control.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, stdout="view_image stable false\n")):
                original = linux_image_runtime(home, "optimized", "# previous tool\n")
                updated = linux_image_runtime(home, "optimized", "# new tool\n")
                unchanged = linux_image_runtime(home, "optimized", "# new tool\n")
            self.assertEqual([result["files_changed"] for result in (original, updated, unchanged)],
                             [True, True, False])
            self.assertEqual(original["registration"], updated["registration"])
            self.assertEqual(updated["registration"], unchanged["registration"])
            self.assertEqual((tool_dir / "server.py").read_text(encoding="utf8"), "# new tool\n")

    def test_restores_only_its_own_native_and_mcp_fields(self):
        initial = b'[features]\nview_image = true\nother = true\n'

        def install(operation, mode, source):
            self.assertEqual(operation, "image_tool")
            self.assertEqual(mode, "optimized")
            compile(source, "server.py", "exec")
            return {"registration": {"command": "/usr/bin/python3", "args": ["-I", "/home/test/.codex/tools/server.py"]},
                    "files_changed": True}

        changed, fields, uninstall, files_changed = linux_image_edits(initial, [], "optimized", install)
        self.assertFalse(uninstall)
        self.assertTrue(files_changed)
        self.assertIn(b"view_image = false", changed)
        self.assertIn(b"[mcp_servers.isolated_vision]", changed)
        self.assertEqual(linux_image_edits(changed, fields, "optimized", install), (None, fields, False, True))
        changed = changed.replace(b"other = true", b"other = false")
        restored, owned, uninstall, files_changed = linux_image_edits(changed, fields, "native", self.fail)
        self.assertTrue(uninstall)
        self.assertFalse(files_changed)
        self.assertIn(b"view_image = true", restored)
        self.assertIn(b"other = false", restored)
        self.assertNotIn(b"isolated_vision", restored)
        self.assertEqual(owned, [])

    def test_stdio_result_contains_only_analysis_text(self):
        tool = ImageTool()
        tool.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "analyze_image", "arguments": {"path": "/home/test/test.png"}, "_meta": {"threadId": "parent"}}}
        with patch("packet_inspector.isolated_image_tool.analyze", return_value="Description") as analyze:
            response = tool.handle(request)
        analyze.assert_called_once_with(request["params"]["arguments"], request["params"]["_meta"])
        self.assertEqual(response["result"], {"content": [{"type": "text", "text": "Description"}], "isError": False})

    def test_analysis_uses_calling_model_and_disables_mcp_in_child(self):
        metadata = {"threadId": "01a190d5-28f6-7163-8cc0-8f1ac253d0f7",
                    "x-codex-turn-metadata": {"model": "caller-model"}}
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory).resolve() / "fixture.png"
            image.write_bytes(TEST_ATTACHMENT)
            # Resolve the POSIX-shaped test path from the fixture's drive on Windows.
            path = image.as_posix().split(":", 1)[-1]

            def run(argv, **options):
                if argv[1:3] == ["mcp", "list"]:
                    listing = [{"name": "isolated_vision", "transport": {"command": "/usr/bin/python3"}}]
                    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(listing))
                self.assertIn("--ephemeral", argv)
                self.assertIn("--model=caller-model", argv)
                self.assertIn("read-only", argv)
                self.assertIn('mcp_servers={"isolated_vision"={enabled=false,command="/usr/bin/python3"}}', argv)
                self.assertIn('responses_api_metadata.codex_inspector_parent_thread_id="' + metadata["threadId"] + '"', argv)
                self.assertEqual(argv[-3:], ["-", "--image", str(image)])
                Path(argv[argv.index("--output-last-message") + 1]).write_text("Only text", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0)

            with chdir(directory), patch.dict(os.environ, {"CODEX_CLI_PATH": "fixture-codex"}), \
                    patch("packet_inspector.isolated_image_tool.subprocess.run", side_effect=run):
                self.assertEqual(analyze({"path": path}, metadata), "Only text")
