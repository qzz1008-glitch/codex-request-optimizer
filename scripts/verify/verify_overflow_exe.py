"""Exercise the packaged overflow fallback against a disposable fake model."""
from __future__ import annotations
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

from tests.test_overflow_compaction import FakeModel, complete, make_body, message

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, default=ROOT / "release")
    release = parser.parse_args().release_dir.resolve()
    local = (ROOT / ".local").resolve()
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="overflow-exe-smoke-", dir=local)
    folder = Path(temporary.name).resolve()
    assert folder.is_relative_to(local)
    config = folder / "config.toml"
    config.write_text('model="test"\nmodel_auto_compact_token_limit=220000\n', encoding="utf8")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    upstream.requests, upstream.fail = [], False
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    process = subprocess.Popen([
        str(release / "CodexPacketInspector.exe"), "--background", "--port", "0",
        "--data-dir", str(folder), "--config", str(config),
        "--upstream", "http://127.0.0.1:" + str(upstream.server_port) + "/v1"],
        cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    opener = build_opener(ProxyHandler({}))
    base, settings = None, None

    def api(path, data=None):
        return json.load(opener.open(Request(base + path, None if data is None else json.dumps(data).encode()), timeout=10))

    def post(items, session="exe-overflow-test", headers=None, **fields):
        client = http.client.HTTPConnection("127.0.0.1", runtime["port"], timeout=30)
        client.request("POST", "/relay/" + settings["relay_token"] + "/responses", make_body(items, **fields),
                       {"Content-Type": "application/json", "session_id": session,
                        "Authorization": "Bearer fake-do-not-cache", **(headers or {})})
        response = client.getresponse()
        status, body = response.status, response.read()
        client.close()
        return status, body

    try:
        for _ in range(200):
            try:
                runtime = json.loads((folder / "runtime.json").read_text())
                settings = json.loads((folder / "settings.json").read_text())
                base = "http://127.0.0.1:" + str(runtime["port"])
                initial = api("/api/status")
                break
            except (OSError, ValueError):
                if process.poll() is not None:
                    raise RuntimeError("Packaged overflow test stopped before startup")
                time.sleep(.1)
        else:
            raise RuntimeError("Packaged overflow test startup timed out")
        original_config = config.read_bytes()
        # Fresh installs keep protection off until the user enables it.
        # Exercise the packaged protection path explicitly below.
        assert initial["packet_size_protection_enabled"] is False
        assert initial["overflow_cache"]["ttl_seconds"] == 7 * 86400
        assert initial["overflow_cache"]["max_entries"] == 256
        assert initial["overflow_cache"]["max_bytes"] == 64 * 1024 * 1024
        assert api("/api/settings", {"packet_size_protection_enabled": False})["packet_size_protection_enabled"] is False
        assert api("/api/settings", {"packet_size_protection_enabled": True})["packet_size_protection_enabled"]
        items = [message(i, 140000) for i in range(18)]
        assert post(items) == (200, complete())
        first_compactions = sum(compact for _, compact, _ in upstream.requests)
        assert first_compactions > 0
        assert post(items + [message("new", 50)]) == (200, complete())
        assert sum(compact for _, compact, _ in upstream.requests) == first_compactions
        assert post([message("reset", 30)]) == (200, complete())
        assert all(size <= 1_000_000 for size, _, _ in upstream.requests)
        upstream.fail = True
        marker = len(upstream.requests)
        status, body = post(items, "exe-failure-test")
        assert status == 502 and json.loads(body)["error"]["code"] == "overflow_compaction_incomplete"
        assert all(compact for _, compact, _ in upstream.requests[marker:])
        for _ in range(100):
            if api("/api/status")["active_requests"] == 0:
                break
            time.sleep(.01)
        packets = api("/api/packets?limit=4")
        assert any(p.get("overflow_cache_hit") for p in packets)
        assert any(p.get("overflow_cache_reset") for p in packets)
        # Exercise the frozen app, not the source imported by this verifier.
        # A version label alone must not hide an old checkpoint implementation.
        upstream.fail = False
        marker = len(upstream.requests)
        reasoning = {"effort": "max", "summary": "detailed", "context": "all_turns"}
        native_fields = {"reasoning": reasoning, "include": ["reasoning.encrypted_content"],
                         "future_protocol": {"preserve": True},
                         "client_metadata": {"native_extension": "preserve"}}
        assert post(items, "exe-lite-test", headers={"X-OpenAI-Internal-Codex-Responses-Lite": "1"},
                    **native_fields) == (200, complete())
        native_requests = upstream.requests[marker:]
        assert any(compact for _, compact, _ in native_requests)
        assert sum(not compact for _, compact, _ in native_requests) == 1
        assert all(size <= 1_000_000 for size, _, _ in native_requests)
        for _, _, value in native_requests:
            assert value["reasoning"] == reasoning
            assert value["include"] == native_fields["include"]
            assert value["future_protocol"] == native_fields["future_protocol"]
            assert value["client_metadata"]["native_extension"] == "preserve"
        marker = len(upstream.requests)
        status, body = post(items, "exe-upstream-error-test",
                            headers={"X-OpenAI-Internal-Codex-Responses-Lite": "1"})
        assert status == 502
        assert "reasoning.context=all_turns" in json.loads(body)["error"]["message"]
        assert all(compact for _, compact, _ in upstream.requests[marker:])
        assert api("/api/status")["maintenance_errors"] == 0
        assert config.read_bytes() == original_config
        assert b"fake-do-not-cache" not in (folder / "overflow-context.sqlite").read_bytes()
        print(json.dumps({"packaged_overflow": "ok", "version": initial["version"],
                          "budget_bytes": 1_000_000, "compaction_calls": first_compactions,
                          "cache_reused": True, "cache_reset": True,
                          "cache_retention_policy_verified": True,
                          "native_template_in_frozen_app_verified": True,
                          "upstream_error_detail_verified": True,
                          "failed_compaction_did_not_send_turn": True, "real_config_changed": False}), flush=True)
    finally:
        if base:
            try:
                api("/api/stop", {})
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
