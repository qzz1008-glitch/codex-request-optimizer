"""Upgrade disposable packaged services while an HTTP response stays open."""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from packet_inspector.router import control_worker, local_json, process_alive, worker_info
from packet_inspector.upgrade import Upgrade, digest, free_port

ROOT = Path(__file__).resolve().parent


def wait_for(function, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            pass
        time.sleep(.1)
    raise RuntimeError("Packaged upgrade verification timed out")


def verify_fresh_install(release, directory):
    data, config = directory / "fresh-data", directory / "fresh-config.toml"
    config.write_text('model="test-only"\n', encoding="utf8")
    port = free_port()
    process = subprocess.Popen([str(release / "CodexPacketInspector.exe"), "--upgrade", "--quiet",
                                "--port", str(port), "--data-dir", str(data), "--config", str(config)],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert process.wait(timeout=100) == 0
        result = json.loads((data / "upgrade-result.json").read_text(encoding="utf8"))
        assert result["state"] == "complete" and result["entry_migrated"]
        assert local_json(port, "/health")["router_protocol"] == 1
        canonical = data / "bin" / "CodexPacketInspector.exe"
        assert digest(canonical) == digest(release / "CodexPacketInspector.exe")
        print(json.dumps({"phase": "fresh_install_with_shared_application", "result": "ok"}), flush=True)
    finally:
        state_path = data / "router-state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf8"))
            runtime = json.loads((data / "runtime.json").read_text(encoding="utf8"))
            local_json(port, "/__router__/stop", {}, state["token"])
            wait_for(lambda: not process_alive(runtime["pid"]), seconds=12)
        for descriptor in (data / "router-workers").glob("*.json"):
            if descriptor.name.endswith(".runtime.json"):
                continue
            runtime_path = descriptor.with_suffix(".runtime.json")
            try:
                control_worker(data, descriptor.stem, "stop")
                runtime = json.loads(runtime_path.read_text(encoding="utf8"))
                wait_for(lambda: not process_alive(runtime["pid"]), seconds=12)
            except (OSError, ValueError, http.client.HTTPException):
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--desktop", action="store_true", help="also verify an open Windows desktop window")
    options = parser.parse_args()
    release = options.release_dir.resolve()
    assert {path.name for path in release.glob("*.exe")} == {
        "CodexPacketInspector.exe", "CodexPacketInspector-Dev.exe"}
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="upgrade-exe-smoke-", dir=local)
    directory = Path(temporary.name).resolve()
    data, config = directory / "data", directory / "config.toml"
    config.write_text('model="test-only"\n', encoding="utf8")
    canonical = data / "bin" / "CodexPacketInspector.exe"
    canonical.parent.mkdir(parents=True)
    shutil.copy2(release / canonical.name, canonical)
    port = free_port()
    started, finish = threading.Event(), threading.Event()
    requests = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, raw, self.headers.get("Authorization")))
            holding = self.headers.get("X-Test-Hold") == "1"
            result = b"first-and-last" if holding else b"complete"
            self.send_response(200)
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result[:1])
            self.wfile.flush()
            if holding:
                started.set()
                finish.wait(100)
            self.wfile.write(result[1:])

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    legacy = subprocess.Popen([str(canonical), "--background", "--port", str(port),
                               "--data-dir", str(data), "--config", str(config), "--upstream",
                               f"http://127.0.0.1:{upstream.server_port}"],
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    updater = None
    held = None
    desktop_process = None
    desktop_pid = None
    observer = Upgrade(directory / "unused", data, config, port)

    def api(path, value=None):
        return local_json(port, path, value, timeout=5)

    def upgrade():
        nonlocal updater
        updater = subprocess.Popen([str(release / "CodexPacketInspector.exe"), "--upgrade", "--quiet",
                                    "--port", str(port), "--data-dir", str(data), "--config", str(config)],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code = updater.wait(timeout=100)
        result = json.loads((data / "upgrade-result.json").read_text(encoding="utf8"))
        if code or result["state"] != "complete":
            raise RuntimeError("Packaged upgrade failed: " + result.get("error", str(code)))
        return result

    try:
        verify_fresh_install(release, directory)
        wait_for(lambda: api("/api/status"))
        api("/api/settings", {"request_compression": "off"})
        original_config = config.read_bytes()
        print(json.dumps({"phase": "legacy_to_stable_entry"}), flush=True)
        migrated = upgrade()
        assert migrated["entry_migrated"] is True
        assert digest(canonical) == digest(release / "CodexPacketInspector.exe")
        assert api("/health")["router_protocol"] == 1
        first = api("/api/status")
        root_pid = json.loads((data / "runtime.json").read_text(encoding="utf8"))["pid"]
        first_worker = first["worker_id"]
        if options.desktop:
            report = data / "desktop-upgrade-check.json"
            report.with_suffix(".hold").write_text("hold", encoding="ascii")
            desktop_process = subprocess.Popen([worker_info(data, first_worker)["executable"],
                                               "--port", str(port), "--data-dir", str(data), "--config", str(config),
                                               "--desktop-check", str(report)],
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready = wait_for(lambda: json.loads(report.read_text(encoding="utf8")), seconds=40)
            assert ready["passed"], ready
            desktop_pid = ready["pid"]
        settings = json.loads((data / "settings.json").read_text(encoding="utf8"))
        path = "/relay/" + settings["relay_token"] + "/responses?fixture=upgrade"
        body = b'{"model":"fixture","input":[]}'
        held = http.client.HTTPConnection("127.0.0.1", port, timeout=100)
        held.request("POST", path, body, {"X-Test-Hold": "1", "Authorization": "Bearer test-upgrade"})
        response = held.getresponse()
        assert response.status == 200 and response.read(1) == b"f"
        assert started.is_set()
        original_packet = api("/api/packets?limit=1")[0]
        assert original_packet["state"] == "streaming"
        print(json.dumps({"phase": "upgrade_during_open_response"}), flush=True)
        updated = upgrade()
        current = api("/api/status")
        assert updated["entry_migrated"] is False
        assert updated["worker_id"] != first_worker
        assert current["worker_id"] == updated["worker_id"]
        if desktop_process:
            desktop_process.wait(timeout=12)
            assert not process_alive(desktop_pid)
            reopened = wait_for(observer._application_windows, seconds=25)
            assert all(window["pid"] != desktop_pid for window in reopened)
            # Allow WebView initialization, then gracefully close only this test profile.
            time.sleep(3)
            observer._close_windows(reopened)
        assert json.loads((data / "runtime.json").read_text(encoding="utf8"))["pid"] == root_pid
        assert process_alive(root_pid) and current["relay_active_requests"] == 1
        assert api("/api/packets?limit=1")[0]["state"] == "streaming"
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        client.request("POST", path, body, {"Authorization": "Bearer test-upgrade"})
        fresh = client.getresponse()
        assert fresh.status == 200 and fresh.read() == b"complete"
        client.close()
        finish.set()
        assert response.read() == b"irst-and-last"
        held.close()
        held = None
        wait_for(lambda: api("/api/status")["relay_active_requests"] == 0)
        assert len(requests) == 2
        assert all(item == ("/responses?fixture=upgrade", body, "Bearer test-upgrade") for item in requests)
        assert config.read_bytes() == original_config
        print(json.dumps({"phase": "waiting_for_old_worker_retirement"}), flush=True)
        state = json.loads((data / "router-state.json").read_text(encoding="utf8"))
        wait_for(lambda: not local_json(port, "/__router__/status", token=state["token"])["draining"], seconds=50)
        packets = api("/api/packets?limit=5")
        assert all(packet["state"] == "complete" for packet in packets)
        print(json.dumps({"phase": "cold_start_restores_stable_entry"}), flush=True)
        state = json.loads((data / "router-state.json").read_text(encoding="utf8"))
        local_json(port, "/__router__/stop", {}, state["token"])
        wait_for(lambda: not process_alive(root_pid), seconds=12)
        worker_runtime = json.loads((data / "router-workers" / (state["active"] + ".runtime.json")).read_text(encoding="utf8"))
        control_worker(data, state["active"], "stop")
        wait_for(lambda: not process_alive(worker_runtime["pid"]), seconds=12)
        launcher = subprocess.Popen([str(canonical), *([] if options.desktop else ["--background"]),
                                     "--port", str(port), "--data-dir", str(data), "--config", str(config)],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        wait_for(lambda: api("/health").get("router_protocol") == 1)
        assert api("/api/status")["router_supported"]
        assert config.read_bytes() == original_config
        if options.desktop:
            reopened = wait_for(observer._application_windows, seconds=30)
            time.sleep(3)
            observer._close_windows(reopened)
            launcher.wait(timeout=10)
        print(json.dumps({"packaged_upgrade": "ok", "version": current["version"],
                          "entry_pid_preserved": True, "port_preserved": True,
                          "open_response_preserved": True, "new_requests_use_new_worker": True,
                          "old_worker_retired": True, "inference_requests": len(requests),
                           "shared_application_entry": True, "fresh_install_verified": True,
                          "running_desktop_reopened": bool(options.desktop),
                           "cold_start_uses_router": True,
                          "provider_config_unchanged_during_upgrade": True, "production_untouched": True}), flush=True)
    finally:
        finish.set()
        if options.desktop:
            marker = data / "desktop-upgrade-check.hold"
            marker.unlink(missing_ok=True)
            observer._close_windows(observer._application_windows())
        if held:
            held.close()
        if updater and updater.poll() is None:
            updater.wait(timeout=100)
        state_file = data / "router-state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf8"))
            try:
                local_json(port, "/__router__/stop", {}, state["token"])
                runtime = json.loads((data / "runtime.json").read_text(encoding="utf8"))
                wait_for(lambda: not process_alive(runtime["pid"]), seconds=10)
            except (OSError, ValueError, RuntimeError):
                pass
        for descriptor in (data / "router-workers").glob("*.json"):
            if descriptor.name.endswith(".runtime.json"):
                continue
            try:
                control_worker(data, descriptor.stem, "stop")
            except (OSError, ValueError, http.client.HTTPException):
                pass
        try:
            api("/api/upgrade/stop", {})
        except (OSError, ValueError, http.client.HTTPException):
            pass
        legacy.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
        # PyInstaller child processes may need a moment to release their files.
        for attempt in range(100):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
