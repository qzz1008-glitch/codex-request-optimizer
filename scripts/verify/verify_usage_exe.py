"""Verify the frozen usage trigger against an isolated fake upstream."""
import argparse
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from tests.test_usage_trigger import Upstream, USAGE, DELTA, request_body, sse, completed

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    release = parser.parse_args().release_dir.resolve()
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="usage-exe-smoke-", dir=local)
    folder = Path(temporary.name)
    config = folder / "config.toml"
    config.write_text('model="test"\nmodel_auto_compact_token_limit=250000\n', encoding="utf8")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.requests, upstream.release = [], threading.Event()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    process = subprocess.Popen([str(release / "CodexPacketInspector.exe"), "--background", "--port", "0",
        "--data-dir", str(folder), "--config", str(config), "--upstream", "http://127.0.0.1:" + str(upstream.server_port)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    opener = build_opener(ProxyHandler({}))
    base = None

    def api(path, value=None):
        request = Request(base + path, None if value is None else json.dumps(value).encode(),
                          {"Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    def post(body, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", runtime["port"], timeout=30)
        try:
            client.request("POST", "/relay/" + settings["relay_token"] + "/responses", body,
                           {"Content-Type": "application/json", "session_id": "usage-exe-test", **(headers or {})})
            response = client.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            client.close()

    try:
        for _ in range(200):
            try:
                runtime = json.loads((folder / "runtime.json").read_text(encoding="utf8"))
                settings = json.loads((folder / "settings.json").read_text(encoding="utf8"))
                base = "http://127.0.0.1:" + str(runtime["port"])
                status = api("/api/status")
                break
            except (OSError, ValueError):
                if process.poll() is not None:
                    raise RuntimeError("Frozen usage test exited before startup")
                time.sleep(.1)
        else:
            raise RuntimeError("Frozen usage test startup timed out")
        config_before = config.read_bytes()
        assert status["packet_size_protection_enabled"] is True
        assert status["native_compact_trigger_bytes"] == 950000
        assert api("/api/settings", {"packet_size_protection_enabled": False})["packet_size_protection_enabled"] is False
        assert api("/api/settings", {"packet_size_protection_enabled": True})["packet_size_protection_enabled"]
        assert post(request_body(compressible=True)) == DELTA + sse(completed())
        assert post(request_body(text_bytes=1_800_000)) == DELTA + sse(completed())
        changed = post(request_body(text_bytes=1_850_000), {"X-Test-Missing-Type": "1"})
        assert changed.startswith(DELTA)
        usage = json.loads(changed[len(DELTA):].split(b"data: ", 1)[1].strip())["response"]["usage"]
        assert usage["input_tokens"] == 500000 and usage["total_tokens"] == 500073
        packet = api("/api/packets?limit=1")[0]
        assert packet["usage_override_applied"] and packet["upstream_usage"] == USAGE
        assert 950000 <= packet["usage_override_request_bytes"] < 1000000
        right = api("/api/packets/" + packet["id"] + "/token-window")
        assert right["input_tokens"] == USAGE["input_tokens"] and right["source"] == "upstream_response"
        assert post(request_body(kind="compaction")) == DELTA + sse(completed())
        custom = api("/api/settings", {"packet_size_protection_enabled": True, "budget_bytes": 700000})
        assert custom["native_compact_trigger_bytes"] == 650000
        changed = post(request_body(text_bytes=1_300_000))
        assert json.loads(changed[len(DELTA):].split(b"data: ", 1)[1].strip())["response"]["usage"]["input_tokens"] == 500000
        from tests.test_overflow_compaction import message
        items = [message(i, 140000) for i in range(18)]
        for cached in (False, True):
            body = json.dumps({"model": "test", "stream": True, "input": items,
                              "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'}}).encode()
            changed = post(body, {"X-Test-Missing-Type": "1"})
            assert json.loads(changed[len(DELTA):].split(b"data: ", 1)[1].strip())["response"]["usage"]["input_tokens"] == 500000
            packet = api("/api/packets?limit=1")[0]
            assert packet["overflow_cache_hit"] == cached
            assert (packet["overflow_compaction_calls"] > 0) != cached
            assert packet["overflow_final_wire_bytes"] <= 700000
            assert packet["usage_override_trigger_bytes"] == 650000
            assert packet["upstream_usage"] == USAGE
            items.append(message("next", 100))
        custom = api("/api/settings", {"budget_bytes": 800000})
        assert custom["native_compact_trigger_bytes"] == 750000
        assert post(request_body(text_bytes=1_300_000)) == DELTA + sse(completed())
        disabled = api("/api/settings", {"packet_size_protection_enabled": False, "budget_bytes": 600000})
        assert disabled["overflow_compaction_enabled"] is False and disabled["native_compact_trigger_enabled"] is False
        assert post(request_body(text_bytes=1_300_000)) == DELTA + sse(completed())
        assert config.read_bytes() == config_before
        print(json.dumps({"packaged_usage_trigger": "ok", "version": status["version"],
                          "trigger_bytes": status["native_compact_trigger_bytes"],
                          "trigger_uses_zstd_wire_bytes": True, "reported_input_tokens": 500000,
                          "right_panel_keeps_original": True, "native_compaction_not_rewritten": True,
                          "missing_content_type_supported": True,
                          "combined_protection_verified": True, "custom_budget_verified": True,
                          "summary_and_cache_still_rewrite_usage": True,
                          "toggle_verified": True, "real_config_changed": False}), flush=True)
    finally:
        upstream.release.set()
        if base:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if api("/api/stop", {}).get("stopped"):
                        break
                except (OSError, http.client.HTTPException):
                    pass
                # The final response may precede its handler's active-count cleanup.
                time.sleep(.05)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=5)
        upstream.shutdown(); upstream.server_close()
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
