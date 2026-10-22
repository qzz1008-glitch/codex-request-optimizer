"""Exercise settings downloads using only the public main EXE in both modes."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from packet_inspector import VERSION
from packet_inspector.frontend_dev import DESKTOP_PROFILE, RESOURCES
from packet_inspector.router import control_worker, local_json, process_alive, worker_info
from packet_inspector.updates import ASSET, Updates
from packet_inspector.upgrade import free_port
from verify_upgrade_exe import wait_for

ROOT = Path(__file__).resolve().parent


def stop_profile(data, port):
    state_path = data / "router-state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf8"))
        runtime = json.loads((data / "runtime.json").read_text(encoding="utf8"))
        local_json(port, "/__router__/stop", {}, state["token"])
        wait_for(lambda: not process_alive(runtime["pid"]), seconds=15)
    for descriptor in (data / "router-workers").glob("*.json"):
        if descriptor.name.endswith(".runtime.json"):
            continue
        try:
            control_worker(data, descriptor.stem, "stop")
            runtime = json.loads(descriptor.with_suffix(".runtime.json").read_text(encoding="utf8"))
            wait_for(lambda: not process_alive(runtime["pid"]), seconds=15)
        except (OSError, ValueError):
            pass


def check_window(executable, data, config, port, development, label, ui_dir=None):
    report = data / (label + ".json")
    arguments = [str(executable), "--port", str(port), "--data-dir", str(data),
                 "--config", str(config), "--desktop-check", str(report)]
    if ui_dir:
        arguments.extend(["--desktop-check-ui", str(ui_dir)])
    process = subprocess.Popen(arguments,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert process.wait(timeout=60) == 0
    result = json.loads(report.read_text(encoding="utf8"))
    assert result.get("passed"), result
    assert result["development"] is development, result
    if ui_dir:
        assert result.get("automatic_refresh"), result


def verify_mode(release, directory, mode):
    data, config = directory / (mode + "-data"), directory / (mode + "-config.toml")
    config.write_text('model="test-only"\n', encoding="utf8")
    port = free_port()
    initial = release / ("CodexPacketInspector-Dev.exe" if mode == "dev" else ASSET)
    try:
        process = subprocess.Popen([str(initial), "--upgrade", "--quiet", "--port", str(port),
                                    "--data-dir", str(data), "--config", str(config)],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert process.wait(timeout=120) == 0
        profile_path = data / DESKTOP_PROFILE
        assert json.loads(profile_path.read_text())["mode"] == mode
        external = None
        if mode == "dev":
            external = data / "editable-ui"
            external.mkdir()
            for name in RESOURCES:
                shutil.copy2(ROOT / "packet_inspector/ui" / name, external / name)
            profile_path.write_text(json.dumps({"mode":"dev", "ui_dir":str(external), "managed_ui":False}), encoding="utf8")
        old = local_json(port, "/api/status")["worker_id"]
        active = worker_info(data, old)
        check_window(Path(active["executable"]), data, config, port, mode == "dev", "before-update", external)

        package = release / ASSET
        with package.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        metadata = {"tag_name":"v" + VERSION, "prerelease":False, "draft":False,
                    "assets":[{"id":123, "name":ASSET, "size":package.stat().st_size, "digest":"sha256:" + checksum}]}
        class LocalDownload:
            def open(self, request, **_kwargs):
                if request.full_url.endswith("/releases/latest"):
                    return io.BytesIO(json.dumps(metadata).encode())
                assert request.full_url.endswith("/releases/assets/123")
                return package.open("rb")
        updates = Updates(data, config, port, current_version="0.0.0", opener=LocalDownload())
        with patch("packet_inspector.updates.github_credential", return_value=None):
            assert updates.check()["state"] == "available"
            assert updates.install()["state"] == "upgrading"
        wait_for(lambda: updates.status()["state"] == "complete", seconds=120)
        after = local_json(port, "/api/status")
        assert after["worker_id"] != old
        assert after["version"] == VERSION
        profile = json.loads(profile_path.read_text())
        assert profile["mode"] == mode
        if external:
            assert Path(profile["ui_dir"]) == external
        worker = worker_info(data, after["worker_id"])
        check_window(Path(worker["executable"]), data, config, port, mode == "dev", "after-update", external)
        assert list((data / "updates/staging").iterdir()) == []
        print(json.dumps({"mode":mode,"settings_download_main_exe":True,"mode_preserved":True,
                          "native_window_verified":True,"staging_cleaned":True}), flush=True)
    finally:
        stop_profile(data, port)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(dir=local, prefix="self-upgrade-exe-check-")
    directory = Path(temporary.name).resolve()
    assert directory.is_relative_to(local.resolve())
    try:
        for mode in ("release", "dev"):
            verify_mode(args.release_dir.resolve(), directory, mode)
    finally:
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
