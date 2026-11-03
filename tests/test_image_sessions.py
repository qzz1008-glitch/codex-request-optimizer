"""Native image conversation continuity and the actual upload boundary."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import unittest

from packet_inspector.analyzer import decode_body, thread_identity
from packet_inspector.image_isolation import ImageIsolationFailure
from packet_inspector.image_session_policy import IMAGE_SESSION_LIMIT, prepare_image_session
from packet_inspector.image_tool_install import ImageToolInstallation
from packet_inspector.transport import PreparedRequest
from tests.test_image_tool_stdio import desktop_cli


class ImageSessionPolicyTests(unittest.TestCase):
    @staticmethod
    def body(text, mode="native"):
        return json.dumps({"client_metadata": {"codex_inspector_request_kind": "image_analysis",
            "codex_inspector_image_session": mode}, "input": [{"role": "user", "content": text}]}).encode()

    def test_large_compressible_conversation_is_losslessly_retained(self):
        body = self.body("visible detail " * 100_000)
        prepared = prepare_image_session(body, {}, "POST", "/responses", PreparedRequest(body, "identity"), "zstd")
        self.assertEqual(decode_body(prepared.body, prepared.encoding), body)
        self.assertEqual(prepared.zstd_level, 16)
        self.assertLess(len(prepared.body), 1_000_000)

    def test_exact_boundary_is_allowed_and_only_explicit_image_sessions_roll_over(self):
        body = self.body("question")
        exact = PreparedRequest(b"x" * 1_000_000, "zstd")
        self.assertIs(prepare_image_session(body, {}, "POST", "/responses", exact, "zstd"), exact)
        oversized = PreparedRequest(b"x" * 1_000_001, "identity")
        with self.assertRaises(ImageIsolationFailure) as result:
            prepare_image_session(body, {}, "POST", "/responses", oversized, "off")
        self.assertEqual(result.exception.code, IMAGE_SESSION_LIMIT)
        self.assertEqual(result.exception.status, 413)
        for method, path, value in [("POST", "/responses", self.body("q", "")),
                                    ("POST", "/images/generations", body), ("GET", "/responses", body)]:
            self.assertIs(prepare_image_session(value, {}, method, path, oversized, "off"), oversized)

    def test_native_nested_metadata_and_compressed_ingress_are_recognized(self):
        import zstandard
        metadata = {"codex_inspector_request_kind": "image_analysis", "codex_inspector_image_session": "native"}
        body = json.dumps({"client_metadata": {"x-codex-turn-metadata": json.dumps(metadata)}}).encode()
        with self.assertRaises(ImageIsolationFailure):
            prepare_image_session(zstandard.ZstdCompressor().compress(body), {"Content-Encoding": "zstd"},
                "POST", "/responses", PreparedRequest(b"x" * 1_000_001, "identity"), "off")


@unittest.skipUnless(os.name == "nt" and desktop_cli(), "Installed Windows Codex CLI required")
class NativeImageSessionTests(unittest.TestCase):
    def test_same_image_follows_up_in_the_same_ephemeral_native_thread(self):
        self.exercise(rollover=False)

    def test_oversized_history_is_not_sent_and_current_question_moves_to_a_new_thread(self):
        self.exercise(rollover=True)

    def exercise(self, *, rollover):
        from PIL import Image
        from packet_inspector.server import InspectorServer

        observed = []
        work_before = set(Path(tempfile.gettempdir()).glob("codex-isolated-vision-*"))
        answer = "First answer"
        if rollover:
            rng = random.Random(211)
            alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ "
            answer = "".join(rng.choices(alphabet, k=1_500_000))

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.reply(b'{"models":[],"plugins":[]}', "application/json")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(decode_body(raw, self.headers.get("Content-Encoding", "identity")))
                observed.append((dict(self.headers.items()), body))
                rid = "image_probe_" + str(len(observed))
                item = {"type": "message", "id": "msg_" + rid, "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": answer if len(observed) == 1 else "Follow-up answer", "annotations": []}]}
                events = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": rid, "status": "completed", "output": [item],
                        "usage": {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}}}]
                self.reply(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events), "text/event-stream")

            def reply(self, data, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory(prefix="native-image-session-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            image = root / "synthetic.png"
            Image.new("RGB", (8, 8), "white").save(image)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream_url = f"http://127.0.0.1:{upstream.server_port}/v1"
            config = home / "config.toml"
            config.write_text('model="gpt-6.1-sol"\nmodel_provider="probe"\nweb_search="disabled"\n'
                '[analytics]\nenabled=false\n[features]\napps=false\nremote_plugin=false\n'
                '[model_providers.probe]\nname="Local probe"\nwire_api="responses"\n'
                f'base_url="{upstream_url}"\nenv_key="IMAGE_PROBE_KEY"\nrequires_openai_auth=false\n'
                'supports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n', encoding="utf8")
            (home / "isolated-vision.config.toml").write_text("", encoding="utf8")
            relay = InspectorServer(("127.0.0.1", 0), root / "data", config, upstream_url)
            relay.settings.update(auto_detect_providers=True, zstd_level=3, image_isolation_enabled=True)
            relay.image_config.state["enabled"] = True
            relay.set_connection(True)
            installation = ImageToolInstallation(home)
            registration = installation.install(str(desktop_cli()))
            for server in (upstream, relay):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("CODEX_") and k not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}}
            env.update(registration["env"], IMAGE_PROBE_KEY="local-only", NO_PROXY="127.0.0.1,localhost",
                       HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="")
            parent = "01a15d9a-f5e3-73c1-b4e7-87b3c5db9a3c"
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}]
            messages += [{"jsonrpc": "2.0", "id": i + 2, "method": "tools/call", "params": {
                "name": "analyze_image", "arguments": {"path": str(image), "instruction": q},
                "_meta": {"threadId": parent}}} for i, q in enumerate(["First question", "Follow-up question"])]
            try:
                process = subprocess.run([registration["command"], *registration["args"]],
                    input="".join(json.dumps(m) + "\n" for m in messages), cwd=root, env=env,
                    capture_output=True, text=True, encoding="utf8", errors="replace", timeout=65,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(process.returncode, 0, process.stderr[-1200:])
                replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
                self.assertEqual(len(replies), 3)
                for reply in replies[1:]:
                    self.assertFalse(reply["result"].get("isError"), reply["result"]["content"][0]["text"][:1200])
                self.assertEqual(replies[-1]["result"]["content"][0]["text"], "Follow-up answer")
                self.assertEqual(len(observed), 2)
                for _, body in observed:
                    images = [part for item in body["input"] for part in item.get("content", [])
                              if isinstance(part, dict) and part.get("type") == "input_image"]
                    self.assertEqual(len(images), 1, "Follow-ups must not append duplicate image attachments")
                ids = [thread_identity(headers, body) for headers, body in observed]
                self.assertTrue(all(ids))
                self.assertEqual(ids[0] != ids[1], rollover)
                self.assertFalse(list(home.glob("sessions/**/*.jsonl")), "Ephemeral image conversations must not write rollouts")
                import sqlite3
                for database in home.glob("state_*.sqlite"):
                    with closing(sqlite3.connect(database)) as c:
                        if c.execute("SELECT 1 FROM sqlite_master WHERE name='threads'").fetchone():
                            self.assertEqual(c.execute("SELECT count(*) FROM threads").fetchone()[0], 0,
                                             "Ephemeral image conversations must not appear in native thread lists")
                self.assertFalse(set(Path(tempfile.gettempdir()).glob("codex-isolated-vision-*")) - work_before,
                                 "Image tool EOF must remove its temporary directory")
                texts = [part.get("text", "") for item in observed[-1][1]["input"]
                         for part in item.get("content", []) if isinstance(part, dict)]
                self.assertEqual(any("First question" in t for t in texts), not rollover)
                records = relay.store.list(limit=10)
                self.assertEqual(sum(r.get("relay_error") == IMAGE_SESSION_LIMIT for r in records), int(rollover))
                self.assertTrue(all(r.get("parent_thread_id") == parent for r in records))
            finally:
                for server in (relay, upstream):
                    server.shutdown()
                    server.server_close()
