"""Verify packaged HTTP fallback with a real Codex CLI and a local-only model."""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.request import Request, urlopen

from packet_inspector import VERSION
from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.image_config import find_codex_cli

ROOT = Path(__file__).resolve().parents[2]


class Model(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.methods.append("GET")
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.methods.append("POST")
        message = {"id": "msg_transport", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
        response = {"id": "resp_transport", "object": "response", "created_at": 1, "status": "completed",
                    "output": [message], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                                                   "input_tokens_details": {"cached_tokens": 0}}}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.added", "output_index": 0,
                   "item": {**message, "status": "in_progress", "content": []}},
                  {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0,
                   "content_index": 0, "delta": "OK"},
                  {"type": "response.output_item.done", "output_index": 0, "item": message},
                  {"type": "response.completed", "response": response}]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def wait_for(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            pass
        time.sleep(.1)
    raise RuntimeError("Timed out waiting for isolated transport fixture")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    codex = find_codex_cli()
    if codex is None:
        raise RuntimeError("A local Codex CLI is required for the native transport verification")
    local = (ROOT / ".local").resolve()
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="transport-release-", dir=local)
    folder = Path(temporary.name).resolve()
    assert folder.is_relative_to(local)
    config, data = folder / "config.toml", folder / "data"
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    upstream.methods = []
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{upstream.server_port}/v1"
    config.write_text('model="gpt-6.1-sol"\nopenai_base_url=' + json.dumps(origin) + "\n", encoding="utf8")
    (folder / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "local-fixture-only"}), encoding="utf8")
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"request_compression": "off",
        "packet_size_protection_enabled": False, "capacity_retry_enabled": False}), encoding="utf8")
    process = subprocess.Popen([str(args.release_dir.resolve() / "CodexPacketInspector.exe"),
        "--background", "--port", "0", "--data-dir", str(data), "--config", str(config), "--upstream", origin],
        cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = None

    def api(path, body=None):
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        runtime = wait_for(lambda: json.loads((data / "runtime.json").read_text("utf8")))
        assert runtime["port"] != 47844
        base = f"http://127.0.0.1:{runtime['port']}"
        assert wait_for(lambda: api("/api/status"))["version"] == VERSION
        results = []
        for provider in ["openai", PROVIDER]:
            if provider == PROVIDER:
                text = config.read_text("utf-8-sig")
                text = ConfigLink._select(text, PROVIDER)
                text += '\n[model_providers.' + PROVIDER + ']\nname="Legacy HTTP"\nwire_api="responses"\n'
                text += 'requires_openai_auth=true\nsupports_websockets=false\nbase_url=' + json.dumps(origin) + "\n"
                config.write_text(text, encoding="utf8")
                wait_for(lambda: "/p/" in tomllib.loads(config.read_text("utf8"))["model_providers"][PROVIDER]["base_url"])
            before = len(upstream.methods)
            env = {**os.environ, "CODEX_HOME": str(folder), "CODEX_API_KEY": "local-fixture-only",
                   "OPENAI_API_KEY": "local-fixture-only"}
            native = subprocess.run([str(codex), "exec", "--ephemeral", "--skip-git-repo-check", "--json",
                "-s", "read-only", "Reply exactly OK. Do not use tools."], cwd=folder, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf8", errors="replace", timeout=60)
            events = [json.loads(line) for line in native.stdout.splitlines() if line.startswith("{")]
            reconnects = sum("reconnecting" in json.dumps(event).lower() for event in events)
            completed = any(event.get("type") == "turn.completed" for event in events)
            result = {"provider": provider, "completed": completed, "exit_code": native.returncode,
                      "reconnect_events": reconnects, "upstream_methods": upstream.methods[before:],
                      "http_fallback": "426 Upgrade Required" in native.stderr}
            assert native.returncode == 0 and completed and reconnects == 0, result
            assert result["upstream_methods"] == ["POST"], result
            if provider == "openai":
                assert result["http_fallback"], result
            results.append(result)
        print(json.dumps({"packaged_transport": "ok", "version": VERSION, "cases": results,
                          "real_sessions_used": False, "real_service_used": False}))
    finally:
        if base:
            try:
                api("/api/stop", {})
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        assert folder.is_relative_to(local)
        temporary.cleanup()


if __name__ == "__main__":
    main()
