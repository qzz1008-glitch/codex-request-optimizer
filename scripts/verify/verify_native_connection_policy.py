"""Exercise installed, unmodified Codex against local synthetic servers only."""
import argparse
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from packet_inspector.analyzer import decode_body
from packet_inspector.server import InspectorServer


def verify(codex):
    observations = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            value = {"models": [], "plugins": []}
            if self.path.split("?", 1)[0].endswith("/accounts/check"):
                value = {"accounts": [{"id": "test-account", "workspace_backend_origin":
                         f"http://127.0.0.1:{self.server.server_port}", "account_routing_override": "NO_CONSTRAINT"}]}
            elif self.path.split("?", 1)[0].endswith("/config/bundle"):
                value = {"requirements_toml": {"enterprise_managed": []}}
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            encoding = self.headers.get("Content-Encoding", "identity")
            json.loads(decode_body(body, encoding))
            observations.append({"encoding": encoding, "bytes": len(body)})
            message = {"type": "message", "id": "msg-test", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "OK"}]}
            events = [{"type": "response.created", "response": {"id": "resp-test", "status": "in_progress"}},
                      {"type": "response.output_item.done", "output_index": 0, "item": message},
                      {"type": "response.completed", "response": {"id": "resp-test", "status": "completed",
                       "output": [message], "usage": {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101}}}]
            data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    with tempfile.TemporaryDirectory(prefix="native-connection-test-") as directory:
        root = Path(directory)
        home = root / "codex-home"
        home.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        url = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        config = home / "config.toml"
        config.write_text(f'model="gpt-6.1-sol"\nopenai_base_url="{url}"\n'
                          f'chatgpt_base_url="http://127.0.0.1:{upstream.server_port}/backend-api/"\n'
                          'cli_auth_credentials_store="file"\n'
                          'web_search="disabled"\n[analytics]\nenabled=false\n'
                          '[features]\napps=false\nremote_plugin=false\n'
                          'enable_request_compression=true\nunbounded_connection_retries=false\n', encoding="utf8")
        b64 = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        token = b64({"alg": "none", "typ": "JWT"}) + "." + b64({"email": "test@example.invalid",
            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}) + ".c2ln"
        (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "tokens": {
            "id_token": token, "access_token": "synthetic-test-token", "refresh_token": "synthetic-test-refresh"},
            "last_refresh": datetime.now(timezone.utc).isoformat()}), encoding="utf8")
        relay = InspectorServer(("127.0.0.1", 0), root / "data", config, url)
        for server in (upstream, relay):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("CODEX_") and key not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}}
            environment.update(CODEX_HOME=str(home), NO_PROXY="127.0.0.1,localhost",
                               HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="")
            results = []
            for connected in (True, False, True, False):
                relay.set_connection(connected)
                before = len(relay.store.list(limit=300))
                process = subprocess.run([str(codex), "-a", "never", "exec", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "danger-full-access", "--color", "never", "--json",
                    "Reply OK. Do not call tools."], cwd=workspace, env=environment,
                    capture_output=True, text=True, encoding="utf8", errors="replace", timeout=45)
                if process.returncode:
                    raise AssertionError("Native Codex test failed: " + (process.stderr or process.stdout)[-1800:])
                assert observations and observations[-1]["encoding"] == "zstd", observations[-1:]
                if connected:
                    packets = relay.store.list(limit=1)
                    meta = relay.store.get(packets[0]["id"])[0]
                    assert meta["wire_bytes"] == meta["decoded_bytes"], "Codex compressed the connected request"
                    assert meta["relay_zstd_level"] == relay.settings["zstd_level"]
                else:
                    assert len(relay.store.list(limit=300)) == before, "Disconnected request still used relay"
                results.append({"connected": connected, "upstream_encoding": observations[-1]["encoding"],
                                "compression_owner": "inspector" if connected else "codex"})
            return results
        finally:
            for server in (relay, upstream):
                server.shutdown()
                server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps({"native_connection_lifecycle": verify(args.codex)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
