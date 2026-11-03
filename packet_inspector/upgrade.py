"""Upgrade application workers while retaining the installed protocol 1 entry."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tempfile

from packet_inspector.entry_port import configured_port
from packet_inspector.locking import file_lock
from packet_inspector.window_lifecycle import EXIT_PROPERTY, request_window_exit
from packet_inspector.frontend_dev import BuildProfile, load_profile, installed_profile, remember_upgrade_profile
from packet_inspector.router import (PROTOCOL, atomic_json, control_worker, healthy_worker,
                                     local_json, process_alive, start_worker, worker_info)
from packet_inspector.sharing_handover import (PROTOCOL as SHARING_HANDOVER_PROTOCOL, UNSUPPORTED as SHARING_UPGRADE_UNSUPPORTED,
                                              active_status as sharing_active, prepare_upgrade, finish_upgrade)
from packet_inspector.startup_diagnostics import STARTUP_SECONDS, startup_environment, wait_for_startup
from packet_inspector.upgrade_compatibility import configuration_upgrade
from packet_inspector.sharing_upgrade import CANCELLED_EXIT, SharingUpgrade, UpgradeCancelled


PHASES = ("validate", "backup", "candidate", "compatibility", "handover", "switch", "verify", "record", "cleanup")
CONTINUITY_UNSUPPORTED = "当前版本暂不支持不中断服务的升级，已保留现有服务。"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class Upgrade:
    total_steps = len(PHASES)

    def __init__(self, payload, data_dir, config, port=None, progress=None, default_profile=None,
                 confirm_interruption=None):
        self.payload, self.data_dir, self.config = Path(payload), Path(data_dir).resolve(), Path(config).resolve()
        self.port = port
        self.progress = progress or (lambda message: None)
        self.completed_steps = 0
        self.canonical = self.data_dir / "bin" / "CodexPacketInspector.exe"
        self.candidate = ""
        self.candidate_process = None
        self.entry_running = False
        self.default_profile = default_profile or BuildProfile()
        self.journal = None
        self.confirm_interruption = confirm_interruption

    def _sharing_record(self, phase, error=None):
        if self.journal is not None:
            record = self.journal.setdefault("sharing_restart", {})
            record["phase"] = phase
            events = record.setdefault("events", [])
            events.append({"phase": phase, "time": time.time(), **({"error": error} if error else {})})
            record["events"] = events[-16:]
            try:
                self._save_journal()
            except OSError:
                self.journal["sharing_journal_warning"] = True

    def _confirm_sharing_restart(self, shared):
        if self.confirm_interruption is None:
            raise ValueError(SHARING_UPGRADE_UNSUPPORTED)
        state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
        plan = SharingUpgrade(self.data_dir, state["active"], shared, self._sharing_record)
        roles = list(plan.snapshot)
        if self.journal is not None:
            self.journal["sharing_restart"] = {"roles": roles}
        self._sharing_record("confirmation_requested")
        if self.confirm_interruption({"roles": roles}) is not True:
            self._sharing_record("cancelled")
            raise UpgradeCancelled("已暂不升级，现有服务保持运行。")
        self._sharing_record("confirmed")
        return plan

    def _stage(self, phase, message):
        if self.journal is not None:
            phases = self.journal["phases"]
            if phases:
                phases[-1].update(state="complete", finished=time.time())
            phases.append({"id": phase, "state": "running", "started": time.time()})
            self.journal["phase"] = phase
        self._report(message, PHASES.index(phase))

    def _report(self, message, completed=None):
        if completed is not None:
            self.completed_steps = max(self.completed_steps, min(completed, self.total_steps))
        if self.journal is not None:
            self.journal.update(completed_steps=self.completed_steps, total_steps=self.total_steps,
                                message=" ".join(str(message).splitlines())[:500])
            # UI messages stay in memory until the commit/error record. Avoid
            # rewriting the shared result file for every line of progress.
            if self.completed_steps == self.total_steps:
                self._save_journal()
        self.progress(message)

    def _save_journal(self):
        # Existing services can read this file without FILE_SHARE_DELETE.
        # Windows then briefly denies atomic replacement. Bound the wait to
        # half a second; other errors retain their original failure semantics.
        for attempt in range(5):
            try:
                atomic_json(self.data_dir / "upgrade-result.json", self.journal)
                return
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(.05 * (attempt + 1))

    def _wait_worker(self, identity, seconds=STARTUP_SECONDS):
        def ready():
            healthy_worker(self.data_dir, identity)
            return True

        def retry(_error):
            if self.candidate_process and self.candidate_process.poll() is not None:
                raise RuntimeError("新版中转未能启动，原服务保持运行。") from None

        wait_for_startup(ready, seconds, errors=(OSError, ValueError, http.client.HTTPException),
            timeout=lambda: RuntimeError("新版中转健康检查超时，原服务保持运行。"), on_retry=retry, chain_error=False)

    def _start_candidate(self, executable, version):
        self.candidate = secrets.token_hex(16)
        atomic_json(self.data_dir / "router-workers" / (self.candidate + ".json"),
                    {"id": self.candidate, "port": free_port(), "token": secrets.token_urlsafe(32),
                     "executable": str(executable), "version": version, "desktop_tray": os.name == "nt"})
        self.candidate_process = start_worker(self.data_dir, self.config, self.candidate, self.port)
        self._wait_worker(self.candidate)

    def _stop_candidate(self):
        if not self.candidate or self._candidate_active() is not False:
            return
        try:
            control_worker(self.data_dir, self.candidate, "stop")
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if self.candidate_process:
            try:
                self.candidate_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass  # Do not force-stop a process that may own a request.

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
                          "--config", str(self.config)], env=startup_environment(),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _running_application_hash(self, status):
        identity = status.get("worker_id")
        if not identity:
            return None  # A legacy service cannot prove that its payload matches.
        worker = worker_info(self.data_dir, identity)
        executable = Path(worker["executable"]).resolve()
        if not executable.is_relative_to((self.data_dir / "bin" / "versions").resolve()):
            raise ValueError("运行中的程序不在版本目录内，未执行升级。")
        return digest(executable)

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

    def _backup(self, *, include_entry=False):
        backup = self.data_dir / "upgrade-backups" / (time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(4))
        backup.mkdir(parents=True)
        paths = [self.config, self.data_dir / "settings.json", self.data_dir / "codex-link.json",
                 self.data_dir / "router-state.json", self.data_dir / "image-tool.json"]
        if include_entry:
            paths.append(self.canonical)
        paths.extend(self.config.parent.glob("*.config.toml"))
        from .image_tool_install import ASSETS, ImageToolInstallation
        tool = ImageToolInstallation(self.config.parent)
        paths.extend(tool.directory / name for name in (*ASSETS, tool.manifest.name))
        manifest = []
        for path in dict.fromkeys(paths):
            saved = backup / (str(len(manifest)) + ".backup")
            exists = path.is_file()
            if exists:
                shutil.copy2(path, saved)
            manifest.append({"path": str(path), "saved": str(saved), "existed": exists,
                             "sha256": digest(saved) if exists else None})
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
                                env=startup_environment(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _wait_entry(self, protocol=None, seconds=STARTUP_SECONDS):
        def ready():
            health = local_json(self.port, "/health", timeout=2)
            status = local_json(self.port, "/api/status", timeout=2)
            if health.get("app") == "codex-packet-inspector" and (protocol is None or health.get("router_protocol") == protocol):
                return status

        return wait_for_startup(ready, seconds, errors=(OSError, ValueError, http.client.HTTPException),
            timeout=lambda: RuntimeError("入口服务健康检查超时。"), chain_error=False)

    def _refresh_integration(self, status):
        self._report("正在更新托盘、快捷方式和启动入口…")
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

    def _install_entry(self, entry_payload, manifest):
        """Install an entry only when preflight proved that no service is running."""
        router_started = False
        applied = {}
        try:
            self.canonical.parent.mkdir(parents=True, exist_ok=True)
            self._copy_retry(entry_payload, self.canonical)
            applied[self.canonical] = digest(self.canonical)
            state_path = self.data_dir / "router-state.json"
            atomic_json(state_path, {"protocol": PROTOCOL, "port": self.port,
                        "token": secrets.token_urlsafe(32), "config": str(self.config),
                        "active": self.candidate, "draining": []})
            applied[state_path] = digest(state_path)
            self._report("启动固定入口并检查前后端…")
            self._start_entry()
            router_started = True
            self.entry_running = True
            status = self._wait_entry(PROTOCOL)
            if status.get("provider_sync_error"):
                raise RuntimeError("接入配置验证失败。")
            self._stage("verify", "正在确认服务与接入状态…")
            self._refresh_integration(status)
            return status
        except Exception:
            self._report("升级未完成，正在恢复旧入口…")
            if router_started:
                try:
                    state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
                    local_json(self.port, "/__router__/stop", {}, state["token"], timeout=3)
                    # Wait for the exact entry executable to release its image.
                    runtime = json.loads((self.data_dir / "runtime.json").read_text(encoding="utf8"))
                    deadline = time.monotonic() + 8
                    while process_alive(runtime["pid"]) and time.monotonic() < deadline:
                        time.sleep(.1)
                    if process_alive(runtime["pid"]):
                        raise RuntimeError("新版入口仍在运行，未覆盖它的程序或配置，请查看升级结果。")
                    self.entry_running = False
                except (OSError, ValueError, KeyError, http.client.HTTPException):
                    raise RuntimeError("新版入口仍在运行，未覆盖它的程序或配置，请查看升级结果。") from None
            # Config migrations have their own conflict-aware rollback. Never
            # restore a whole settings snapshot over a concurrent user edit.
            for entry in manifest:
                path = Path(entry["path"])
                if path not in applied:
                    continue
                if not path.exists() or digest(path) != applied[path]:
                    raise RuntimeError("升级期间入口文件被修改，已保留当前文件与备份。") from None
                if entry["existed"]:
                    self._copy_retry(entry["saved"], path)
                else:
                    path.unlink(missing_ok=True)
            raise

    def _run(self):
        sharing_restart = None
        self._stage("validate", "正在校验升级包…")
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
        self._report("升级包校验通过，正在检查当前服务…")
        if self.port is None:
            runtime_file = self.data_dir / "runtime.json"
            self.port = json.loads(runtime_file.read_text(encoding="utf8"))["port"] if runtime_file.exists() else configured_port(self.data_dir)
        if not isinstance(self.port, int) or not 1024 <= self.port <= 65535:
            raise ValueError("入口端口无效。")
        legacy = False
        health = None
        status = {}
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
            if not legacy and health.get("router_protocol") != PROTOCOL:
                raise ValueError("入口协议不兼容，未修改正在运行的服务。")
            # This check precedes candidate construction, window changes and
            # stopping a legacy process. Unknown sharing owners are preserved.
            if status.get("sharing_settings_supported"):
                shared = status if "sharing_runtime" in status else local_json(self.port, "/api/sharing", timeout=3)
                if sharing_active(shared) and (legacy or status.get("sharing_handover_protocol") != SHARING_HANDOVER_PROTOCOL):
                    if legacy:
                        raise ValueError(SHARING_UPGRADE_UNSUPPORTED)
                    sharing_restart = self._confirm_sharing_restart(local_json(self.port, "/api/sharing", timeout=3))
            if legacy:
                # Even an idle direct relay owns its listening port. Replacing
                # it would create an outage for the next request.
                raise ValueError(CONTINUITY_UNSUPPORTED)
            self.entry_running = True
        self._stage("backup", "正在备份本次更新涉及的配置…")
        backup, backup_manifest = self._backup(include_entry=health is None)
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
        self._stage("candidate", "正在启动新版服务并检查运行状态…")
        success, reopen = False, False
        sharing_ticket, old, candidate_released = None, None, False
        compatibility = ExitStack()
        try:
            self._start_candidate(version_exe, manifest["version"])
            self._stage("compatibility", "正在检查并更新兼容配置…")
            migrations = compatibility.enter_context(configuration_upgrade(self.data_dir, self.config))
            control_worker(self.data_dir, self.candidate, "upgrade_prepare")
            self._stage("handover", "正在准备交接服务，现有请求继续运行…")
            if health:
                state = json.loads((self.data_dir / "router-state.json").read_text(encoding="utf8"))
                old = state["active"]
                if status.get("sharing_handover_protocol") == SHARING_HANDOVER_PROTOCOL:
                    self._report("正在交接额度与网络共享，已有连接继续完成…")
                    sharing_ticket = prepare_upgrade(self.data_dir, old, self.candidate)
                elif status.get("sharing_settings_supported"):
                    shared = local_json(self.port, "/api/sharing", timeout=3)
                    if sharing_active(shared) and sharing_restart is None:
                        sharing_restart = self._confirm_sharing_restart(shared)
                    if sharing_restart is not None and sharing_restart.source != old:
                        raise ValueError("共享服务已变化，请重新执行升级。")
                baseline = local_json(self.port, "/api/status", timeout=3)
                if sharing_restart is not None:
                    control_worker(self.data_dir, self.candidate, "upgrade_pause")
                    self._report("正在暂停旧版共享，准备切换服务…")
                    sharing_restart.pause(old, check=True)
                if windows:
                    self._report("正在更新桌面窗口，固定入口继续转发…")
                    reopen = True
                    self._close_windows(windows)
                self._stage("switch", "正在切换服务，请稍候…" if sharing_restart else "正在交接新请求，已有连接继续完成…")
                try:
                    local_json(self.port, "/__router__/switch", {"worker_id": self.candidate, "expected_active": old}, state["token"], timeout=40)
                    status = self._wait_entry(PROTOCOL, seconds=8)
                    if status.get("worker_id") != self.candidate or status.get("provider_sync_error"):
                        raise ValueError("切换后验证失败。")
                    if sharing_restart is not None:
                        self._report("正在等待旧请求结束并退出旧版共享服务…")
                        sharing_restart.stop_source()
                        self._report("正在恢复额度与网络共享…")
                        sharing_restart.restore(self.candidate)
                        status = self._wait_entry(PROTOCOL, seconds=8)
                    self._stage("verify", "正在确认服务、接入与借用状态…")
                    self._verify_continuity(baseline, status)
                    self._refresh_integration(status)
                    if sharing_restart is not None:
                        control_worker(self.data_dir, self.candidate, "upgrade_resume")
                except Exception:
                    if not self._presentation_committed():
                        if self._candidate_active() is not False:
                            if sharing_restart is not None:
                                sharing_restart.pause(self.candidate)
                                control_worker(self.data_dir, self.candidate, "upgrade_pause", release_provider=True)
                                candidate_released = True
                            self._rollback_switch(old, state["token"])
                    raise
            else:
                self._stage("switch", "正在启用服务入口…")
                self._install_entry(entry, backup_manifest)
            if sharing_ticket is not None:
                ticket, sharing_ticket = sharing_ticket, None
                finish_upgrade(self.data_dir, old, self.candidate, ticket)
            compatibility.close()
            if sharing_restart is not None:
                self._sharing_record("complete")
            success = True
            return {"state": "complete", "version": manifest["version"], "port": self.port,
                    "worker_id": self.candidate, "backup": str(backup), "migrations": migrations,
                    "entry_migrated": health is None}
        except BaseException:
            if self._candidate_active() is not False:
                # If rollback cannot restore the old owner, retain the active
                # relay and its compatible configuration, rather than undoing
                # files underneath a service that is already serving requests.
                compatibility.close()
            else:
                compatibility.__exit__(*sys.exc_info())
            if sharing_restart is not None and sharing_restart.paused:
                try:
                    active = self._candidate_active()
                    if active is None:
                        raise RuntimeError("无法确认当前共享进程。")
                    if not active and not candidate_released:
                        # A failed standby must not undo the provider restored
                        # below when it closes after rollback.
                        sharing_restart.pause(self.candidate)
                        control_worker(self.data_dir, self.candidate, "upgrade_pause", release_provider=True)
                    sharing_restart.restore(self.candidate if active else old)
                    if active:
                        control_worker(self.data_dir, self.candidate, "upgrade_resume")
                    self._sharing_record("recovered")
                except Exception as exc:
                    self._sharing_record("recovery_failed", {"type": type(exc).__name__, "message": str(exc)[:250]})
                    raise RuntimeError("共享恢复未完成，请查看升级结果并重新开启共享。") from exc
            raise
        finally:
            try:
                if sharing_ticket is not None:
                    finish_upgrade(self.data_dir, old, self.candidate, sharing_ticket)
            finally:
                if not success:
                    self._stop_candidate()
                if reopen:
                    try:
                        self._open_window()
                    except (OSError, ValueError, RuntimeError):
                        if self.journal is not None:
                            self.journal["window_refresh_warning"] = True

    def _candidate_active(self):
        path = self.data_dir / "router-state.json"
        if not path.exists():
            return None if self.entry_running else False
        try:
            return json.loads(path.read_text(encoding="utf8"))["active"] == self.candidate
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _verify_continuity(before, after):
        for key in ("connection_state", "sharing_runtime"):
            if key in before and before[key] != after.get(key):
                raise ValueError("升级后服务状态不一致，正在恢复原服务。")

    def run(self):
        self.completed_steps = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.data_dir / "upgrade.lock", blocking=False):
            self.journal = {"state": "running", "started": time.time(), "phases": []}
            self._save_journal()
            result = None
            try:
                result = self._run()
                # The verified service handover is the commit boundary. Later
                # bookkeeping or cleanup must never roll it back or report a
                # working upgrade as failed.
                self.journal.update(result)
                self._stage("record", "正在保存更新结果…")
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
                self.journal.update(result)
                self._save_journal()
                self._stage("cleanup", "正在清理旧版本文件，服务已正常运行…")
                from .upgrade_cleanup import cleanup_versions
                try:
                    result["cleanup"] = cleanup_versions(self.data_dir)
                    if result["cleanup"].get("skipped"):
                        result["cleanup_warning"] = result["cleanup"]["skipped"]
                except Exception as exc:
                    result["cleanup_warning"] = type(exc).__name__
                self.journal.update(result, finished=time.time())
                self.journal["phases"][-1].update(
                    state="warning" if result.get("cleanup_warning") else "complete", finished=time.time())
                self._report("升级完成，部分旧文件将在后续清理。" if result.get("cleanup_warning")
                             else "升级完成。旧服务会在请求结束后退出。", self.total_steps)
            except Exception as exc:
                if result is None:
                    self.journal.update(state="cancelled" if isinstance(exc, UpgradeCancelled) else "failed",
                                        error=str(exc)[:500], finished=time.time())
                    if self.journal["phases"]:
                        self.journal["phases"][-1].update(state=self.journal["state"], finished=time.time())
                    try:
                        self._save_journal()
                    except OSError:
                        pass
                    raise
                result["finalization_warning"] = type(exc).__name__
                self.journal.update(result, finished=time.time(), completed_steps=self.total_steps)
                self.completed_steps = self.total_steps
                try:
                    self._save_journal()
                except OSError:
                    pass
                self.progress("升级完成，部分收尾操作未完成。")
            return result


def run_integrated(arguments, quiet=False, force=False):
    """A downloaded application is also its own verified upgrade payload."""
    from packet_inspector import VERSION
    from packet_inspector.server import argument_parser
    from packet_inspector.updates import version_tuple
    options = argument_parser().parse_args(arguments)
    directory = options.data_dir.resolve()
    engine = Upgrade(directory, directory, options.config, options.port)
    source_hash = digest(sys.executable)
    report = {"started": time.time(), "version": VERSION, "source_executable": str(Path(sys.executable).resolve())[:1000],
              "source_sha256": source_hash}
    def record(state, **values):
        report.update(state=state, updated=time.time(), **values)
        try:
            atomic_json(directory / "launch-status.json", report)
        except OSError:
            pass  # A diagnostic write must not prevent opening the application.
    record("checking")
    reason = "service_not_running"
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
        report.update(installed_version=status["version"], active_worker_id=status.get("worker_id", ""))
        if current_version >= version_tuple(VERSION) and not force and engine.canonical.is_file():
            installed_hash = engine._running_application_hash(status) if current_version == version_tuple(VERSION) else None
            report["installed_sha256"] = installed_hash
            if current_version > version_tuple(VERSION) or source_hash == installed_hash:
                record("opening_installed", reason="newer_installed" if current_version > version_tuple(VERSION) else "same_payload")
                if not quiet:
                    engine._open_window()
                record("complete", action="open_installed")
                return 0
        if current_version > version_tuple(VERSION):
            raise ValueError("已安装更新的版本，未执行降级。")
        reason = "same_version_payload_changed" if current_version == version_tuple(VERSION) else "newer_payload"
        if force:
            reason = "explicit_upgrade"
    record("preparing_upgrade", action="upgrade", reason=reason)
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
        result = main(args, default_profile=load_profile(), open_after=not quiet)
        record("cancelled" if result == CANCELLED_EXIT else "complete" if result == 0 else "failed", exit_code=result)
        return result


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
    from packet_inspector.upgrade_window import run_upgrade_window
    engine = Upgrade(payload, args.data_dir, args.config, args.port, default_profile=default_profile)
    return run_upgrade_window(engine, open_after=open_after)


if __name__ == "__main__":
    raise SystemExit(main())
