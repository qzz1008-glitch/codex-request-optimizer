"""Upgrade application workers while retaining the installed protocol 1 entry."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile

from packet_inspector.entry_port import configured_port
from packet_inspector.app_icon import APP_ICON
from packet_inspector.locking import file_lock
from packet_inspector.window_lifecycle import EXIT_PROPERTY, request_window_exit
from packet_inspector.frontend_dev import BuildProfile, load_profile, installed_profile, remember_upgrade_profile
from packet_inspector.router import (PROTOCOL, atomic_json, control_worker, healthy_worker,
                                     local_json, process_alive, start_worker, worker_info)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class Upgrade:
    def __init__(self, payload, data_dir, config, port=None, progress=None, default_profile=None):
        self.payload, self.data_dir, self.config = Path(payload), Path(data_dir).resolve(), Path(config).resolve()
        self.port = port
        self.progress = progress or (lambda message: None)
        self.canonical = self.data_dir / "bin" / "CodexPacketInspector.exe"
        self.candidate = ""
        self.candidate_process = None
        self.default_profile = default_profile or BuildProfile()

    def _wait_worker(self, identity, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                healthy_worker(self.data_dir, identity)
                return
            except (OSError, ValueError, http.client.HTTPException):
                if self.candidate_process and self.candidate_process.poll() is not None:
                    raise RuntimeError("新版中转未能启动，原服务保持运行。") from None
                time.sleep(.1)
        raise RuntimeError("新版中转健康检查超时，原服务保持运行。")

    def _start_candidate(self, executable, version):
        self.candidate = secrets.token_hex(16)
        atomic_json(self.data_dir / "router-workers" / (self.candidate + ".json"),
                    {"id": self.candidate, "port": free_port(), "token": secrets.token_urlsafe(32),
                     "executable": str(executable), "version": version, "desktop_tray": os.name == "nt"})
        self.candidate_process = start_worker(self.data_dir, self.config, self.candidate, self.port)
        self._wait_worker(self.candidate)

    def _stop_candidate(self):
        if not self.candidate:
            return
        state = self.data_dir / "router-state.json"
        if state.exists():
            try:
                if json.loads(state.read_text(encoding="utf8")).get("active") == self.candidate:
                    return  # A failed rollback must never shut down the active relay.
            except (OSError, ValueError, TypeError):
                return  # Unclear ownership is not permission to stop a worker.
        try:
            control_worker(self.data_dir, self.candidate, "stop")
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if self.candidate_process:
            try:
                self.candidate_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass  # Do not force-stop a process that may own a request.

    def _legacy_processes(self):
        if os.name != "nt":
            return []
        # Return only identity and a mode flag. Never echo command lines.
        code = ("$taskPath=$env:INSPECTOR_UPGRADE_EXE; Get-CimInstance Win32_Process | "
                "Where-Object {$_.ExecutablePath -eq $taskPath} | "
                "ForEach-Object {[pscustomobject]@{pid=$_.ProcessId; background=($_.CommandLine -match '--background')}} | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", code],
                                env={**os.environ, "INSPECTOR_UPGRADE_EXE": str(self.canonical)},
                                capture_output=True, encoding="utf8", errors="replace", timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("无法确认旧程序状态，升级未中断现有中转。")
        values = json.loads(result.stdout or "[]")
        return values if isinstance(values, list) else [values]

    def _close_windows(self, processes):
        if os.name != "nt" or not processes:
            return
        import ctypes
        from ctypes import wintypes
        pids = {item["pid"] for item in processes if not item["background"]}
        requested = set()
        controlled_pid = None
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
        user.SetPropW.restype = wintypes.BOOL
        @callback_type
        def close_window(handle, _parameter):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value in pids:
                title = ctypes.create_unicode_buffer(256)
                user.GetWindowTextW(handle, title, len(title))
                window_class = ctypes.create_unicode_buffer(256)
                user.GetClassNameW(handle, window_class, len(window_class))
                if not title.value.startswith("Codex") or not window_class.value.startswith("WindowsForms"):
                    return True  # Leave bootloader and message-only windows alone.
                # Ordinary close goes to the tray. Upgrades explicitly close only
                # this managed optimizer window, leaving forwarding independent.
                if user.SetPropW(handle, EXIT_PROPERTY, 1):
                    requested.add(pid.value)
                    user.PostMessageW(handle, 0x10, 0, 0)
            return True
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        deadline = time.monotonic() + 25
        refresh_at = 0
        while any(process_alive(pid) for pid in pids) and time.monotonic() < deadline:
            profile = installed_profile(self.data_dir)
            if request_window_exit(self.data_dir) and process_alive(profile.get("pid")):
                controlled_pid = profile["pid"]
            if controlled_pid and not process_alive(controlled_pid):
                return
            if requested and all(not process_alive(pid) for pid in requested):
                return  # Onefile loaders may finish cleaning up after the GUI.
            if time.monotonic() >= refresh_at:
                # Onefile parents appear before their GUI children. Discover the
                # child as it starts instead of sending WM_CLOSE to the loader.
                pids.update(item["pid"] for item in self._application_windows() if not item["background"])
                refresh_at = time.monotonic() + 1
            user.EnumWindows(close_window, 0)
            time.sleep(.2)
        if controlled_pid and not process_alive(controlled_pid):
            return
        if requested and all(not process_alive(pid) for pid in requested):
            return
        if any(process_alive(pid) for pid in pids):
            raise RuntimeError("旧版优化器窗口未退出，请关闭窗口后再升级。中转仍在运行。")

    def _open_window(self):
        subprocess.Popen([str(self.canonical), "--port", str(self.port), "--data-dir", str(self.data_dir),
                          "--config", str(self.config)], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _application_windows(self):
        if os.name != "nt":
            return []
        profile = installed_profile(self.data_dir)
        code = ("$taskVersions=$env:INSPECTOR_UPGRADE_VERSIONS; $taskData=$env:INSPECTOR_UPGRADE_DATA; "
                "$taskPattern='--data-dir\\s+\"?'+[regex]::Escape($taskData)+'\"?(?:\\s|$)'; "
                "Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath "
                "-and $_.Name -in @('CodexPacketInspector.exe','CodexPacketInspector-Dev.exe') "
                "-and $_.ExecutablePath -ne $env:INSPECTOR_UPGRADE_SELF "
                "-and $_.ExecutablePath -ne $env:INSPECTOR_UPGRADE_ENTRY "
                "-and $_.CommandLine -notmatch '--background|--worker-id|--upgrade' "
                "-and ($_.CommandLine "
                "-or ($_.ProcessId -eq $env:INSPECTOR_UPGRADE_PID -and $_.ExecutablePath -eq $env:INSPECTOR_UPGRADE_GUI)) -and ("
                "$_.ExecutablePath.StartsWith($taskVersions,[StringComparison]::OrdinalIgnoreCase) "
                "-or ($_.ProcessId -eq $env:INSPECTOR_UPGRADE_PID -and $_.ExecutablePath -eq $env:INSPECTOR_UPGRADE_GUI) "
                "-or $_.CommandLine -match $taskPattern "
                "-or ($env:INSPECTOR_UPGRADE_DEFAULT -eq '1' -and $_.CommandLine -notmatch '--data-dir' "
                "-and $_.Name -in @('CodexPacketInspector.exe','CodexPacketInspector-Dev.exe')))} | "
                "ForEach-Object {[pscustomobject]@{pid=$_.ProcessId; background=$false; executable=$_.ExecutablePath; "
                "mode=$(if($_.Name -eq 'CodexPacketInspector-Dev.exe'){'dev'}else{$env:INSPECTOR_UPGRADE_MODE})}} "
                "| ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", code],
                                env={**os.environ, "INSPECTOR_UPGRADE_VERSIONS": str(self.data_dir / "bin" / "versions") + os.sep,
                                     "INSPECTOR_UPGRADE_DATA": str(self.data_dir),
                                     "INSPECTOR_UPGRADE_SELF": str(Path(sys.executable).resolve()),
                                     "INSPECTOR_UPGRADE_ENTRY": str(self.canonical) if (self.data_dir / "router-state.json").is_file() else "",
                                     "INSPECTOR_UPGRADE_PID": str(profile.get("pid", 0)),
                                     "INSPECTOR_UPGRADE_GUI": str(profile.get("executable", "")),
                                     "INSPECTOR_UPGRADE_MODE": profile.get("mode", "release"),
                                     "INSPECTOR_UPGRADE_DEFAULT": "1" if self.data_dir == (Path.home() / ".codex-packet-inspector").resolve() else "0"},
                                capture_output=True, encoding="utf8", errors="replace", timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("无法确认桌面窗口状态，未切换服务。")
        values = json.loads(result.stdout or "[]")
        return values if isinstance(values, list) else [values]

    def _backup(self):
        backup = self.data_dir / "upgrade-backups" / (time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(4))
        backup.mkdir(parents=True)
        paths = [self.config, self.data_dir / "settings.json", self.data_dir / "codex-link.json",
                 self.data_dir / "router-state.json", self.canonical]
        paths.extend(self.config.parent.glob("*.config.toml"))
        manifest = []
        for path in dict.fromkeys(paths):
            saved = backup / (str(len(manifest)) + ".backup")
            exists = path.is_file()
            if exists:
                shutil.copy2(path, saved)
            manifest.append({"path": str(path), "saved": str(saved), "existed": exists})
        atomic_json(backup / "manifest.json", manifest)
        return backup, manifest

    @staticmethod
    def _copy_retry(source, target):
        for attempt in range(50):
            try:
                shutil.copy2(source, target)
                return
            except OSError:
                if attempt == 49:
                    raise
                time.sleep(.1)

    def _start_entry(self):
        return subprocess.Popen([str(self.canonical), "--background", "--tray", "--port", str(self.port),
                                 "--data-dir", str(self.data_dir), "--config", str(self.config)],
                                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _wait_entry(self, protocol=None, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                health = local_json(self.port, "/health", timeout=2)
                status = local_json(self.port, "/api/status", timeout=2)
                if health.get("app") == "codex-packet-inspector" and (protocol is None or health.get("router_protocol") == protocol):
                    return status
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(.1)
        raise RuntimeError("入口服务健康检查超时。")

    def _refresh_integration(self, status):
        self.progress("正在更新托盘、快捷方式和启动入口…")
        control_worker(self.data_dir, self.candidate, "installation_refresh")
        if status.get("desktop_refresh_supported"):
            try:
                control_worker(self.data_dir, self.candidate, "desktop_refresh")
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                if not self._presentation_committed():
                    raise
                worker = healthy_worker(self.data_dir, self.candidate)
                current = local_json(worker["port"], "/api/status", timeout=3)
                if current.get("worker_id") != self.candidate or (worker.get("desktop_tray") and not current.get("desktop_tray")):
                    raise RuntimeError("托盘交接状态待确认，已保留当前中转。") from None

    def _presentation_committed(self):
        try:
            return worker_info(self.data_dir, self.candidate).get("presentation_committed") is True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _rollback_switch(self, identity, token):
        data = {"worker_id": identity, "expected_active": self.candidate}
        try:
            return local_json(self.port, "/__router__/switch", data, token, timeout=40)
        except (OSError, ValueError, http.client.HTTPException):
            state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
            if state["active"] != self.candidate:
                raise
            # A slow native refresh may outlive the old worker's drain grace.
            runtime_path = self.data_dir / "router-workers" / (identity + ".runtime.json")
            if not runtime_path.exists() or process_alive(json.loads(runtime_path.read_text(encoding="utf8")).get("pid")):
                raise
            process = start_worker(self.data_dir, self.config, identity, self.port)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("旧后台未能恢复，已保留当前中转。") from None
                try:
                    healthy_worker(self.data_dir, identity)
                    return local_json(self.port, "/__router__/switch", data, token, timeout=40)
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(.1)
            raise RuntimeError("旧后台恢复超时，已保留当前中转。") from None

    def _migrate(self, entry_payload, legacy):
        processes = self._legacy_processes() if legacy else []
        had_window = any(not item["background"] for item in processes)
        if had_window:
            self.progress("正在关闭旧版优化器窗口，中转继续运行…")
            self._close_windows(processes)
        backup, manifest = self._backup()
        stopped, router_started = False, False
        try:
            if legacy:
                self.progress("首次安装固定入口，正在等待旧服务空闲…")
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    current = local_json(self.port, "/api/status", timeout=3)
                    if current.get("relay_active_requests", current.get("active_requests", 0)) == 0:
                        try:
                            local_json(self.port, "/api/upgrade/stop", {}, timeout=3)
                            stopped = True
                            break
                        except (OSError, ValueError, http.client.HTTPException):
                            pass
                    time.sleep(.25)
                if not stopped:
                    raise RuntimeError("旧服务仍有请求，已取消本次升级。现有中转继续运行。")
                for item in processes:
                    deadline = time.monotonic() + 8
                    while process_alive(item["pid"]) and time.monotonic() < deadline:
                        time.sleep(.1)
                    if process_alive(item["pid"]):
                        raise RuntimeError("旧服务尚未退出，取消替换。")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        probe.settimeout(.3)
                        if probe.connect_ex(("127.0.0.1", self.port)) != 0:
                            break
                    time.sleep(.1)
                else:
                    raise RuntimeError("旧监听端口未释放，取消替换。")
            self.canonical.parent.mkdir(parents=True, exist_ok=True)
            self._copy_retry(entry_payload, self.canonical)
            atomic_json(self.data_dir / "router-state.json", {"protocol": PROTOCOL, "port": self.port,
                        "token": secrets.token_urlsafe(32), "config": str(self.config),
                        "active": self.candidate, "draining": []})
            self.progress("启动固定入口并检查前后端…")
            self._start_entry()
            router_started = True
            status = self._wait_entry(PROTOCOL)
            if status.get("provider_sync_error"):
                raise RuntimeError("接入配置验证失败。")
            if legacy:
                control_worker(self.data_dir, self.candidate, "recover", owners=["relay"])
            self._refresh_integration(status)
            if had_window:
                self._open_window()
            return backup
        except Exception:
            self.progress("升级未完成，正在恢复旧入口…")
            if router_started:
                try:
                    state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
                    local_json(self.port, "/__router__/stop", {}, state["token"], timeout=3)
                    # Wait for the exact entry executable to release its image.
                    runtime = json.loads((self.data_dir / "runtime.json").read_text(encoding="utf8"))
                    deadline = time.monotonic() + 8
                    while process_alive(runtime["pid"]) and time.monotonic() < deadline:
                        time.sleep(.1)
                except (OSError, ValueError, KeyError, http.client.HTTPException):
                    raise RuntimeError("新版入口仍在运行，未覆盖它的程序或配置，请查看升级结果。") from None
            self._stop_candidate()
            for entry in manifest:
                path = Path(entry["path"])
                if entry["existed"]:
                    self._copy_retry(entry["saved"], path)
                else:
                    path.unlink(missing_ok=True)
            if stopped:
                self._start_entry()
                self._wait_entry()
            if had_window:
                self._open_window()
            raise

    def _run(self):
        manifest = json.loads((self.payload / "manifest.json").read_text(encoding="utf8"))
        if manifest.get("router_protocol") != PROTOCOL:
            raise ValueError("升级包的入口协议不兼容。")
        app = self.payload / "CodexPacketInspector.exe"
        entry_mode = manifest.get("entry_mode", "standalone")
        if entry_mode not in {"shared", "standalone"}:
            raise ValueError("升级包的入口模式不兼容。")
        # Existing upgrade payloads used a separate entry binary.
        entry = app if entry_mode == "shared" else self.payload / "CodexInspectorRouter.exe"
        for path in dict.fromkeys((app, entry)):
            if digest(path) != manifest["sha256"][path.name]:
                raise ValueError("升级包校验失败。")
        self.progress("升级包校验通过，正在准备新版前后端…")
        if self.port is None:
            runtime_file = self.data_dir / "runtime.json"
            self.port = json.loads(runtime_file.read_text(encoding="utf8"))["port"] if runtime_file.exists() else configured_port(self.data_dir)
        if not isinstance(self.port, int) or not 1024 <= self.port <= 65535:
            raise ValueError("入口端口无效。")
        legacy = False
        health = None
        try:
            health = local_json(self.port, "/health", timeout=2)
            status = local_json(self.port, "/api/status", timeout=3)
        except (ConnectionError, OSError, http.client.HTTPException):
            with socket.socket() as probe:
                probe.settimeout(.5)
                occupied = probe.connect_ex(("127.0.0.1", self.port)) == 0
            if health is not None or occupied:
                raise RuntimeError("现有服务暂不可用，未替换入口。") from None
        else:
            if (health.get("app") != "codex-packet-inspector" or Path(status["data_dir"]).resolve() != self.data_dir
                    or Path(status["profile"]["config_path"]).resolve() != self.config):
                raise ValueError("端口属于其他服务或配置，未修改它。")
            legacy = health.get("router_protocol") is None
            if legacy and not status.get("upgrade_drain_supported"):
                raise ValueError("旧版不支持安全排空，请先手动关闭旧中转再运行升级程序。")
            if not legacy and health.get("router_protocol") != PROTOCOL:
                raise ValueError("入口协议不兼容，未修改正在运行的服务。")
        windows = self._application_windows()
        remember_upgrade_profile(self.data_dir, self.default_profile, windows)
        version_dir = self.data_dir / "bin" / "versions" / manifest["sha256"][app.name]
        version_dir.mkdir(parents=True, exist_ok=True)
        version_exe = version_dir / app.name
        if not version_exe.exists():
            staging = version_dir / (app.name + ".new")
            shutil.copy2(app, staging)
            staging.replace(version_exe)
        if digest(version_exe) != manifest["sha256"][app.name]:
            raise ValueError("已暂存的新版程序校验失败。")
        success, backup, reopen = False, None, False
        try:
            self._start_candidate(version_exe, manifest["version"])
            if health and not legacy:
                if windows:
                    self.progress("正在更新桌面窗口，固定入口继续转发…")
                    self._close_windows(windows)
                    reopen = True
                state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
                self.progress("新版已就绪，正在切换新请求；旧请求继续完成…")
                old = state["active"]
                local_json(self.port, "/__router__/switch", {"worker_id": self.candidate, "expected_active": old}, state["token"], timeout=40)
                try:
                    status = self._wait_entry(PROTOCOL, seconds=8)
                    if status.get("worker_id") != self.candidate or status.get("provider_sync_error"):
                        raise ValueError("切换后验证失败。")
                    self._refresh_integration(status)
                except Exception:
                    if not self._presentation_committed():
                        self._rollback_switch(old, state["token"])
                    raise
            else:
                backup = self._migrate(entry, legacy)
            success = True
            self.progress("升级完成。旧服务会在请求结束后退出。")
            return {"state": "complete", "version": manifest["version"], "port": self.port,
                    "worker_id": self.candidate, "backup": str(backup) if backup else None,
                    "entry_migrated": health is None or legacy}
        finally:
            if not success:
                self._stop_candidate()
            if reopen:
                self._open_window()

    def run(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.data_dir / "upgrade.lock", blocking=False):
            atomic_json(self.data_dir / "upgrade-result.json", {"state": "running", "started": time.time()})
            try:
                result = self._run()
                runtime_path = self.data_dir / "runtime.json"
                if runtime_path.exists():
                    try:
                        runtime = json.loads(runtime_path.read_text(encoding="utf8"))
                        if not isinstance(runtime, dict):
                            raise ValueError("invalid entry runtime")
                        if runtime.get("port") == self.port:
                            atomic_json(runtime_path, {**runtime, "version": result["version"], "worker_id": self.candidate})
                    except (OSError, ValueError, TypeError):
                        result["runtime_refresh_warning"] = True
                from .upgrade_cleanup import cleanup_versions
                result["cleanup"] = cleanup_versions(self.data_dir)
                atomic_json(self.data_dir / "upgrade-result.json", {**result, "finished": time.time()})
                return result
            except Exception as exc:
                atomic_json(self.data_dir / "upgrade-result.json", {"state": "failed", "error": str(exc)[:500], "finished": time.time()})
                raise


def run_integrated(arguments, quiet=False, force=False):
    """A downloaded application is also its own verified upgrade payload."""
    from packet_inspector import VERSION
    from packet_inspector.server import argument_parser
    from packet_inspector.updates import version_tuple
    options = argument_parser().parse_args(arguments)
    directory = options.data_dir.resolve()
    engine = Upgrade(directory, directory, options.config, options.port)
    try:
        health = local_json(options.port, "/health", timeout=2)
        status = local_json(options.port, "/api/status", timeout=3)
    except (ConnectionError, OSError, http.client.HTTPException):
        pass
    else:
        if (health.get("app") != "codex-packet-inspector" or Path(status.get("data_dir", "")).resolve() != directory
                or Path(status.get("profile", {}).get("config_path", "")).resolve() != options.config.resolve()):
            raise ValueError("此端口未使用当前配置，未执行升级。")
        current_version = version_tuple(status["version"])
        if current_version >= version_tuple(VERSION) and not force and engine.canonical.is_file():
            if not quiet:
                engine._open_window()
            return 0
        if current_version > version_tuple(VERSION):
            raise ValueError("已安装更新的版本，未执行降级。")
    staging = directory / "updates" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="application-", dir=staging) as name:
        payload = Path(name).resolve()
        if not payload.is_relative_to(staging.resolve()):
            raise ValueError("升级暂存目录无效。")
        app = payload / "CodexPacketInspector.exe"
        shutil.copy2(sys.executable, app)
        atomic_json(payload / "manifest.json", {"version": VERSION, "router_protocol": PROTOCOL,
                    "entry_mode": "shared", "sha256": {app.name: digest(app)}})
        args = ["--payload-dir", str(payload), "--data-dir", str(directory), "--config", str(options.config.resolve()),
                "--port", str(options.port)]
        if quiet:
            args.append("--quiet")
        return main(args, default_profile=load_profile(), open_after=not quiet)


def main(argv=None, *, default_profile=None, open_after=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".codex-packet-inspector")
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--port", type=int)
    parser.add_argument("--payload-dir", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    payload = args.payload_dir or Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent)) / "upgrade-payload"
    if args.quiet:
        try:
            result = Upgrade(payload, args.data_dir, args.config, args.port, default_profile=default_profile).run()
            if sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception:
            return 1
    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    try:
        window.iconbitmap(str(APP_ICON))
    except tk.TclError:
        pass
    window.title("Codex 请求优化器 · 安装与升级")
    window.geometry("540x230")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="正在更新前后端", font=("Microsoft YaHei UI", 14)).pack(anchor="w")
    message = tk.StringVar(value="准备升级包…")
    ttk.Label(frame, textvariable=message, wraplength=480).pack(anchor="w", pady=16)
    bar = ttk.Progressbar(frame, mode="indeterminate")
    bar.pack(fill="x")
    bar.start(12)
    engine = Upgrade(payload, args.data_dir, args.config, args.port, default_profile=default_profile)
    open_app = ttk.Button(frame, text="打开优化器", command=engine._open_window, state="disabled")
    open_app.pack(side="left", pady=18)
    close = ttk.Button(frame, text="关闭", command=window.destroy, state="disabled")
    close.pack(anchor="e", pady=18)
    updates = queue.Queue()
    engine.progress = updates.put
    finished = threading.Event()
    succeeded = threading.Event()
    def work():
        try:
            engine.run()
            if open_after:
                engine._open_window()
            succeeded.set()
        except Exception as exc:
            updates.put("升级未完成：" + str(exc))
        finally:
            finished.set()
    def refresh():
        while not updates.empty():
            message.set(updates.get_nowait())
        if finished.is_set():
            if succeeded.is_set() and open_after:
                window.destroy()
                return
            bar.stop()
            close.configure(state="normal")
            if succeeded.is_set():
                open_app.configure(state="normal")
        else:
            window.after(150, refresh)
    window.protocol("WM_DELETE_WINDOW", lambda: window.destroy() if finished.is_set() else None)
    threading.Thread(target=work, daemon=True).start()
    refresh()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
