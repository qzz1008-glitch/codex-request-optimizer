"""Verify the unmodified Codex binary against isolated local capacity failures."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from packet_inspector.analyzer import decode_body
from packet_inspector.server import InspectorServer


def verify(codex, case):
    requests, times = [], []
    def message(ident, text):
        return {"type":"message", "id":ident, "role":"assistant", "status":"completed",
                "content":[{"type":"output_text", "text":text, "annotations":[]}]}

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply(200, b'{"models":[],"plugins":[]}', "application/json")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append(json.loads(decode_body(body, self.headers.get("Content-Encoding", "identity"))))
            times.append(time.monotonic())
            fail = len(requests) == 1 or case == "exhausted"
            error = {"code":"server_is_overloaded", "message":"Selected model is at capacity. Please try a different model."}
            if case == "http" and fail:
                self.reply(503, json.dumps({"error":error}).encode(), "application/json")
                return
            rid = "failed" if fail else "success"
            events = [{"type":"response.created", "response":{"id":rid,"status":"in_progress"}}]
            if fail:
                if case == "partial":
                    events.append({"type":"response.output_item.done", "output_index":0,
                                   "item":message("before", "BEFORE-CAPACITY")})
                events.append({"type":"error", **error} if case == "bare_error" else {
                    "type":"response.failed", "response":{"id":rid, "status":"failed", "error":error}})
            else:
                item = message("after", "AFTER-RETRY")
                events.extend([{"type":"response.output_item.done", "output_index":0, "item":item},
                               {"type":"response.completed", "response":{"id":rid, "status":"completed", "output":[item],
                                "usage":{"input_tokens":100,"output_tokens":2,"total_tokens":102}}}])
            data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
            self.reply(200, data, "text/event-stream")

        def reply(self, status, body, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with tempfile.TemporaryDirectory(prefix="inspector-native-retry-") as directory:
        root = Path(directory)
        home, workspace, data = root/"home", root/"workspace", root/"data"
        for path in (home, workspace, data):
            path.mkdir()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream_url = f"http://127.0.0.1:{upstream.server_port}/v1"
        config = home/"config.toml"
        config.write_text('model="gpt-6.1-sol"\nmodel_provider="probe"\nweb_search="disabled"\n'
                          'check_for_update_on_startup=false\n[analytics]\nenabled=false\n'
                          '[features]\napps=false\nremote_plugin=false\nunbounded_connection_retries=false\n'
                          f'[model_providers.probe]\nname="local probe"\nbase_url="{upstream_url}"\n'
                          'wire_api="responses"\nenv_key="INSPECTOR_NATIVE_RETRY_TEST_KEY"\n'
                          'requires_openai_auth=false\nsupports_websockets=false\n', encoding="utf8")
        (data/"settings.json").write_text(json.dumps({"capacity_retry_enabled":True, "capacity_retry_max_retries":2,
            "capacity_retry_delay_seconds":1, "packet_size_protection_enabled":False, "zstd_level":0,
            "auto_detect_providers":False}), encoding="utf8")
        relay = InspectorServer(("127.0.0.1", 0), data, config, upstream_url)
        for server in (upstream, relay):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            relay.set_connection(True)
            environment = {key:value for key,value in os.environ.items()
                           if not key.startswith("CODEX_") and key not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "GH_TOKEN", "GITHUB_TOKEN"}}
            environment.update(CODEX_HOME=str(home), INSPECTOR_NATIVE_RETRY_TEST_KEY="synthetic-local-only",
                               NO_PROXY="127.0.0.1,localhost", HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="")
            process = subprocess.run([str(codex), "-a", "never", "exec", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--color", "never", "--json", "Reply AFTER-RETRY. Do not call tools."],
                cwd=workspace, env=environment, capture_output=True, text=True, encoding="utf8", errors="replace", timeout=45)
            expected = 3 if case == "exhausted" else 2
            assert len(requests) == expected, (case, len(requests), process.stderr[-1200:], process.stdout[-1200:])
            assert (process.returncode == 0) == (case != "exhausted"), (case, process.stderr[-1200:])
            gaps = [round(b-a, 2) for a,b in zip(times, times[1:])]
            assert all(gap >= .9 for gap in gaps), (case, gaps)
            packets = relay.store.list(limit=20)
            assert len(packets) == expected, (case, len(packets))
            assert all(packet.get("capacity_retry_count", 0) == 0 for packet in packets), case
            if case == "partial":
                assert any(c.get("text") == "BEFORE-CAPACITY" for item in requests[1]["input"]
                           for c in item.get("content", [])), "native retry omitted a completed item"
            return {"case":case, "native_requests":len(requests), "relay_self_retries":0,
                    "gaps_seconds":gaps, "result":"stopped_at_limit" if case == "exhausted" else "success"}
        finally:
            for server in (relay, upstream):
                server.shutdown()
                server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True, type=Path)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda case: verify(args.codex, case), ("partial", "bare_error", "http", "exhausted")))
    print(json.dumps({"native_retry":results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
