"""GitHub Releases delivery. Credentials stay in memory and never enter the bundle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request, build_opener,
                            getproxies)

from . import VERSION
from .locking import file_lock
from .router import atomic_json

ASSET = "CodexPacketInspector.exe"
LEGACY_ASSET = "CodexRequestOptimizer-Upgrade.exe"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value))
    if not match:
        raise ValueError("版本号格式无效。")
    return tuple(int(part) for part in match.groups())


def github_credential():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    paths = [shutil.which("gh")]
    for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if root:
            paths.extend([str(Path(root) / "Programs/GitHub CLI/bin/gh.exe"), str(Path(root) / "GitHub CLI/gh.exe")])
    executable = next((path for path in paths if path and Path(path).is_file()), None)
    if executable:
        try:
            result = subprocess.run([executable, "auth", "token", "--hostname", "github.com"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.scheme != "https" or target.username or target.password or target.port not in {None, 443}
                or target.hostname not in {"api.github.com", "github.com", "release-assets.githubusercontent.com",
                                           "objects.githubusercontent.com", "github-releases.githubusercontent.com"}):
            raise ValueError("更新下载跳转到了不受支持的地址。")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and target.hostname != urlsplit(req.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def github_opener():
    from urllib import request
    proxies = getproxies()
    if not (set(proxies) - {"no"}):
        registry = getattr(request, "getproxies_registry", None)
        if registry:
            proxies = {**registry(), **proxies}
    return build_opener(ProxyHandler(proxies), GitHubRedirect())


class Updates:
    def __init__(self, directory, config, port, current_version=VERSION, *, opener=None):
        self.directory, self.config = Path(directory), Path(config)
        self.port, self.current_version = port, current_version
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings_path, self.state_path = self.directory / "updates.json", self.directory / "update-state.json"
        channel = json.loads(Path(__file__).with_name("update_channel.json").read_text(encoding="utf8"))
        self.defaults = {"repository": channel["repository"], "automatic": False}
        self.opener = opener or github_opener()
        self.guard, self.task = threading.Lock(), None
        self.next_poll = 0

    def settings(self):
        saved = json.loads(self.settings_path.read_text(encoding="utf8")) if self.settings_path.exists() else {}
        return {**self.defaults, **saved}

    def cached(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {"state": "idle"}

    def status(self):
        cached = self.cached()
        if cached.get("state") == "available" and version_tuple(cached["latest_version"]) <= version_tuple(self.current_version):
            cached = {key: value for key, value in cached.items() if key != "asset"}
            cached["state"] = "current"
            atomic_json(self.state_path, cached)
        result_path = self.directory / "upgrade-result.json"
        if cached.get("state") == "upgrading" and result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf8"))
            if result.get("finished", 0) >= cached.get("upgrade_started", 0):
                cached = {**cached, "state": "complete" if result.get("state") == "complete" else "error",
                          "error": result.get("error", ""), "finished": result.get("finished")}
                atomic_json(self.state_path, cached)
        return {**self.settings(), **{key: value for key, value in cached.items() if key != "asset"},
                "current_version": self.current_version, "supported": True}

    def configure(self, data):
        current = self.settings()
        repository = data.get("repository", current["repository"])
        if not isinstance(repository, str):
            raise ValueError("仓库格式应为 owner/repository。")
        repository = repository.strip()
        if not REPOSITORY.fullmatch(repository) or repository.count("/") != 1 or ".." in repository:
            raise ValueError("仓库格式应为 owner/repository。")
        automatic = data.get("automatic", current["automatic"])
        if not isinstance(automatic, bool):
            raise ValueError("自动升级设置必须为布尔值。")
        atomic_json(self.settings_path, {"repository": repository, "automatic": automatic})
        if repository != current["repository"]:
            atomic_json(self.state_path, {"state": "idle"})
        self.next_poll = 0
        return self.status()

    def _request(self, url, token="", binary=False):
        if not url.startswith("https://api.github.com/"):
            raise ValueError("无效的 GitHub API 地址。")
        headers = {"User-Agent": "CodexRequestOptimizer/" + self.current_version,
                   "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2026-03-10"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.opener.open(Request(url, headers=headers), timeout=30)

    def check(self):
        repository = self.settings()["repository"]
        token = ""
        api = "https://api.github.com/repos/" + repository + "/releases/latest"
        try:
            response = self._request(api)
        except HTTPError as exc:
            if exc.code not in {401, 403, 404}:
                raise
            token = github_credential()
            if not token:
                raise ValueError("仓库尚无正式版本，或私有仓库需要先登录 GitHub CLI。") from None
            response = self._request(api, token)
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("版本信息过大。")
        release = json.loads(raw)
        latest = str(release["tag_name"]).removeprefix("v")
        newer = version_tuple(latest) > version_tuple(self.current_version)
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("更新源返回了非正式版本。")
        result = {"state": "available" if newer else "current", "latest_version": latest,
                  "checked": time.time(), "repository": repository, "error": "",
                  "release_url": "https://github.com/" + repository + "/releases/tag/v" + latest}
        if newer:
            asset = next((item for item in release.get("assets", []) if item.get("name") == ASSET), None)
            if asset is None:
                asset = next((item for item in release.get("assets", []) if item.get("name") == LEGACY_ASSET), None)
            if not asset or not isinstance(asset.get("digest"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"]):
                raise ValueError("新版缺少带 SHA-256 校验的升级 EXE。")
            if not isinstance(asset.get("id"), int) or not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= 300 * 1024 * 1024:
                raise ValueError("升级包信息无效。")
            result["asset"] = {"id": asset["id"], "name": asset["name"], "size": asset["size"], "sha256": asset["digest"][7:]}
        atomic_json(self.state_path, result)
        return result

    def install(self):
        cached = self.cached()
        if cached.get("repository") != self.settings()["repository"] or cached.get("state") not in {"available", "downloading"}:
            raise ValueError("请先检查更新。")
        if version_tuple(cached["latest_version"]) <= version_tuple(self.current_version):
            raise ValueError("当前版本无需升级，请重新检查。")
        asset = cached["asset"]
        folder = self.directory / "updates" / cached["latest_version"]
        folder.mkdir(parents=True, exist_ok=True)
        asset_name = asset.get("name", ASSET)
        if asset_name not in {ASSET, LEGACY_ASSET}:
            raise ValueError("更新文件名无效。")
        executable = folder / asset_name
        atomic_json(self.state_path, {**cached, "state": "downloading"})
        if not executable.exists() or self._hash(executable) != asset["sha256"]:
            temporary = executable.with_suffix(".download")
            try:
                token = github_credential()
                url = "https://api.github.com/repos/" + cached["repository"] + "/releases/assets/" + str(asset["id"])
                digest, received = hashlib.sha256(), 0
                with self._request(url, token, binary=True) as response, temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > asset["size"]:
                            raise ValueError("升级包大小与版本信息不符。")
                        stream.write(chunk)
                        digest.update(chunk)
                if received != asset["size"] or digest.hexdigest() != asset["sha256"]:
                    raise ValueError("升级包 SHA-256 校验失败。")
                temporary.replace(executable)
            finally:
                temporary.unlink(missing_ok=True)
        started = time.time()
        atomic_json(self.state_path, {**cached, "state": "upgrading", "upgrade_started": started})
        subprocess.Popen([str(executable), *(["--upgrade"] if asset_name == ASSET else []), "--quiet", "--data-dir", str(self.directory.resolve()),
                          "--config", str(self.config.resolve()), "--port", str(self.port)],
                         env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self.status()

    @staticmethod
    def _hash(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def start(self, action):
        if action not in {"check", "install"}:
            raise ValueError("无效的更新操作。")
        with self.guard:
            if self.task and self.task.is_alive():
                return self.status()
            def run():
                try:
                    with file_lock(self.directory / "update-check.lock", blocking=False):
                        if action == "check":
                            atomic_json(self.state_path, {**self.cached(), "state": "checking", "error": "", "attempted": time.time()})
                            checked = self.check()
                            if self.settings()["automatic"] and checked["state"] == "available":
                                self.install()
                        else:
                            self.install()
                except BlockingIOError:
                    pass
                except Exception as exc:
                    # Never include request objects or headers in persisted errors.
                    error = str(exc)[:300] if isinstance(exc, ValueError) else "检查或升级失败：" + type(exc).__name__
                    atomic_json(self.state_path, {**self.cached(), "state": "error", "error": error})
            self.task = threading.Thread(target=run, daemon=True)
            self.task.start()
        return self.status()

    def poll(self):
        if time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 60
        cached = self.cached()
        if (self.settings()["automatic"] and time.time() - cached.get("checked", 0) >= 6 * 3600
                and time.time() - cached.get("attempted", 0) >= 900):
            self.start("check")
