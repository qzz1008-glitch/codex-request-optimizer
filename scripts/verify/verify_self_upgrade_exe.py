"""Run the published v0.6.14 updater against the formal release files."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import types
from unittest.mock import patch

from packet_inspector import VERSION
from packet_inspector.frontend_dev import DESKTOP_PROFILE, RESOURCES
from packet_inspector.router import control_worker, local_json, process_alive, worker_info
from packet_inspector.updates import ASSET
from packet_inspector.upgrade import Upgrade, free_port
from scripts.verify.verify_upgrade_exe import close_test_desktop, verify_presentation, wait_for
from scripts.build_inspector import PUBLIC_FILES

ROOT = Path(__file__).resolve().parents[2]


def previous_updater():
    # Preserve the old asset selection and command unchanged. __file__ resolves
    # the shared release channel, just as it did in the original application.
    source = ROOT / "tests" / "fixtures" / "updates_v0_6_14.py"
    module = types.ModuleType("packet_inspector.previous_updates_fixture")
    module.__package__ = "packet_inspector"
    module.__file__ = str(ROOT / "packet_inspector" / "updates.py")
    exec(compile(source.read_text(encoding="utf8"), str(source), "exec"), module.__dict__)
    return module


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
    initial = release / "CodexPacketInspector-Dev.exe" if mode == "dev" else release / "publish" / ASSET
    try:
        process = subprocess.Popen([str(initial), "--upgrade", "--quiet", "--port", str(port),
                                    "--data-dir", str(data), "--config", str(config)],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert process.wait(timeout=120) == 0
        profile_path = data / DESKTOP_PROFILE
        assert json.loads(profile_path.read_text())["mode"] == mode
        before = local_json(port, "/api/status")
        verify_presentation(data, before)
        # The tray preloads the single desktop host. The probe needs to own it,
        # and the editable profile must not race with that host's startup write.
        close_test_desktop(data)
        external = None
        if mode == "dev":
            external = data / "editable-ui"
            external.mkdir()
            for name in RESOURCES:
                shutil.copy2(ROOT / "packet_inspector/ui" / name, external / name)
            profile_path.write_text(json.dumps({"mode":"dev", "ui_dir":str(external), "managed_ui":False}), encoding="utf8")
        old = before["worker_id"]
        active = worker_info(data, old)
        report = data / "old-window.json"
        report.with_suffix(".hold").write_text("hold", encoding="ascii")
        old_window = subprocess.Popen([active["executable"], "--port", str(port), "--data-dir", str(data),
                                       "--config", str(config), "--desktop-check", str(report)],
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = wait_for(lambda: json.loads(report.read_text(encoding="utf8")), seconds=45)
        assert ready["passed"], ready
        old_pid = ready["pid"]

        package = release / "publish" / ASSET
        with package.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        previous = previous_updater()
        assets = []
        for index, name in enumerate(PUBLIC_FILES, 123):
            path = release / "publish" / name
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            assets.append({"id":index, "name":name, "size":path.stat().st_size, "digest":"sha256:" + digest})
        metadata = {"tag_name":"v" + VERSION, "prerelease":False, "draft":False,
                    "assets":list(reversed(assets))}
        class LocalDownload:
            def open(self, request, **_kwargs):
                if request.full_url.endswith("/releases/latest"):
                    return io.BytesIO(json.dumps(metadata).encode())
                assert request.full_url.endswith("/releases/assets/123")
                return package.open("rb")
        updates = previous.Updates(data, config, port, current_version="0.6.14", opener=LocalDownload())
        with patch.object(previous, "github_credential", return_value=None):
            available = updates.check()
            assert available["state"] == "available" and available["asset"]["name"] == ASSET
            assert available["asset"]["sha256"] == checksum
            assert updates.install()["state"] == "upgrading"
        wait_for(lambda: updates.status()["state"] == "complete", seconds=120)
        assert old_window.wait(timeout=30) == 0 and not process_alive(old_pid)
        after = local_json(port, "/api/status")
        assert after["worker_id"] != old
        assert after["version"] == VERSION
        profile = json.loads(profile_path.read_text())
        assert profile["mode"] == mode
        if external:
            assert Path(profile["ui_dir"]) == external
        def reopened():
            value = json.loads(profile_path.read_text())
            return value.get("pid") not in {None, old_pid} and process_alive(value["pid"])
        wait_for(reopened, seconds=40)
        observer = Upgrade(data, data, config, port)
        observer._close_windows(wait_for(observer._application_windows, seconds=30))
        worker = worker_info(data, after["worker_id"])
        check_window(Path(worker["executable"]), data, config, port, mode == "dev", "after-update", external)
        assert list((data / "updates/staging").iterdir()) == []
        print(json.dumps({"mode":mode,"original_v0_6_14_updater":True,"formal_release_selected":True,
                          "old_window_closed_new_window_opened":True,"mode_preserved":True,
                          "native_window_verified":True,"staging_cleaned":True}), flush=True)
    finally:
        try:
            observer = Upgrade(data, data, config, port)
            observer._close_windows(observer._application_windows())
        finally:
            stop_profile(data, port)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("release", "dev"), help="rerun one mode after a failed verification")
    args = parser.parse_args()
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(dir=local, prefix="self-upgrade-exe-check-")
    directory = Path(temporary.name).resolve()
    assert directory.is_relative_to(local.resolve())
    try:
        for mode in ((args.mode,) if args.mode else ("release", "dev")):
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
