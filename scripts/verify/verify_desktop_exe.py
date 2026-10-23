"""Verify the packaged desktop UI in isolated release and development profiles."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen

from tests.test_packet_inspector import RelayTests

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_CHECKS = ("single_title_bar", "native_resize_border", "window_buttons")


def verify(executable, mode):
    RelayTests.setUpClass()
    try:
        data = RelayTests.server.data_dir
        profile = {"mode": mode}
        frontend = data / "frontend"
        if mode == "dev":
            shutil.copytree(ROOT / "packet_inspector" / "ui", frontend)
            profile.update(ui_dir=str(frontend), managed_ui=False)
        (data / "desktop-profile.json").write_text(json.dumps(profile), encoding="utf8")
        report = data / "desktop-check.json"
        command = [str(executable), "--port", str(RelayTests.server.port),
                   "--data-dir", str(data), "--config", str(RelayTests.config),
                   "--desktop-check", str(report)]
        if mode == "dev":
            command += ["--desktop-check-ui", str(frontend)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=100,
                                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not report.is_file():
            raise RuntimeError("desktop did not produce its verification report")
        outcome = json.loads(report.read_text(encoding="utf8"))
        missing = [key for key in REQUIRED_CHECKS if outcome.get(key) is not True]
        if result.returncode or not outcome.get("passed") or missing:
            return {"mode": mode, "passed": False, "missing_checks": missing,
                    "error": outcome.get("error", "packaged desktop is missing the current title bar")}
        with urlopen(f"http://127.0.0.1:{RelayTests.server.port}/health", timeout=3) as response:
            assert json.load(response)["app"] == "codex-packet-inspector"
        return {"mode": mode, "passed": True, "title_bar": True,
                "window_controls": True, "relay_preserved": True}
    finally:
        RelayTests.tearDownClass()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args(argv)
    executable = (args.executable or args.release_dir / "CodexPacketInspector.exe").resolve()
    if not executable.is_file():
        parser.error("desktop executable does not exist")
    results = []
    for mode in ("release", "dev"):
        try:
            results.append(verify(executable, mode))
        except Exception as exc:
            results.append({"mode": mode, "passed": False, "error": type(exc).__name__ + ": " + str(exc)[:300]})
        if not results[-1]["passed"]:
            break
    passed = len(results) == 2 and all(item["passed"] for item in results)
    print(json.dumps({"passed": passed, "desktop_profiles": results}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
