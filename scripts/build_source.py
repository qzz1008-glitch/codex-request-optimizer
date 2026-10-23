"""GitHub Flow source checks, frozen inputs, and verifiable EXE provenance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


class SourceError(ValueError):
    pass


def git(root, *arguments, check=True):
    result = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), *arguments], capture_output=True,
                            encoding="utf8", errors="replace", timeout=35)
    if check and result.returncode:
        raise SourceError(result.stderr.strip()[:500] or "Git source check failed")
    return result.stdout.rstrip("\r\n") if check else result


def product_path(name):
    path = Path(name)
    return (bool(path.parts) and path.parts[0] in {"packet_inspector", "scripts", "tests", "docs", ".githooks", ".github"}
            or name in {"README.md", "LICENSE", "AGENTS.md", ".gitignore", ".gitattributes", "requirements-dev.txt",
                        ".github/RELEASE_NOTES.md", ".github/workflows/windows-build.yml",
                        "build_inspector.py", "dev_inspector_ui.py", "build_app_icon.ps1"}
            or (len(path.parts) == 1 and (name.startswith(("test_", "verify_"))
                                         or name.endswith("_inspector_service.ps1"))))


def dirty_paths(root):
    entries = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    paths = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        name = entry[3:]
        if product_path(name):
            paths.append(name)
        if "R" in entry[:2] or "C" in entry[:2]:
            index += 1
    return sorted(set(paths))


def version(text):
    match = re.search(r'^VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.M)
    if not match:
        raise SourceError("Source VERSION is missing or invalid")
    return tuple(map(int, match.groups()))


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        records = git(self.root, "worktree", "list", "--porcelain", "-z").split("\0\0")
        self.trees = []
        for record in records:
            values = dict(line.split(" ", 1) for line in record.split("\0") if " " in line)
            if "worktree" in values and "HEAD" in values:
                self.trees.append(values)
        if not self.trees:
            raise SourceError("Builds require the shared Git repository")
        self.canonical = Path(self.trees[0]["worktree"]).resolve()
        common = Path(git(self.root, "rev-parse", "--git-common-dir"))
        self.common = (self.root / common).resolve()

    def validate(self, mode, *, refresh=False):
        if mode not in ("dev", "release"):
            raise SourceError("Invalid build mode")
        if mode == "release" and self.root != self.canonical:
            raise SourceError(f"Release only from the shared main directory {self.canonical}; merge this task branch first")
        if git(self.root, "diff", "--name-only", "--diff-filter=U"):
            raise SourceError("Resolve all source merge conflicts before building")
        head = git(self.root, "rev-parse", "HEAD")
        pending = dirty_paths(self.root)
        branch = git(self.root, "branch", "--show-current")
        if mode == "release":
            if pending:
                raise SourceError("Release source must be committed: " + ", ".join(pending[:5]))
            if branch != "main" and os.environ.get("GITHUB_REF_TYPE") != "tag":
                raise SourceError("Release builds must use the shared main branch or its CI tag")
        elif self.root != self.canonical:
            if git(self.canonical, "branch", "--show-current") != "main":
                raise SourceError("Return the shared integration directory to main before another worktree builds")
            if dirty_paths(self.canonical):
                raise SourceError("Shared main has uncommitted source; integrate it before building a task branch")
        if refresh:
            git(self.root, "fetch", "--quiet", "origin", "main")
        main = git(self.root, "rev-parse", "--verify", "refs/heads/main", check=False)
        if main.returncode == 0:
            if git(self.root, "merge-base", "--is-ancestor", main.stdout.strip(), head, check=False).returncode:
                raise SourceError("Task branch is behind shared main; merge or rebase main before building")
        upstream = git(self.root, "rev-parse", "--verify", "origin/main", check=False)
        if upstream.returncode == 0:
            if git(self.root, "merge-base", "--is-ancestor", upstream.stdout.strip(), head, check=False).returncode:
                raise SourceError("Build source is behind origin/main; integrate the latest main first")
            floor = version(git(self.root, "show", "origin/main:packet_inspector/__init__.py"))
            current = version((self.root / "packet_inspector/__init__.py").read_text(encoding="utf8"))
            if current < floor:
                raise SourceError("Source version is older than origin/main")
        if mode == "release" and branch != "main":
            if upstream.returncode or head != upstream.stdout.strip():
                raise SourceError("CI release tag must point at the latest origin/main commit")
        main_ref = "refs/heads/main" if main.returncode == 0 else "refs/remotes/origin/main"
        main_commit = (main.stdout.strip() if main.returncode == 0 else
                       upstream.stdout.strip() if upstream.returncode == 0 else "")
        return {"git_commit": head, "branch": branch, "dirty": bool(pending),
                "main_ref": main_ref, "main_commit": main_commit,
                "version": ".".join(map(str, version((self.root / "packet_inspector/__init__.py").read_text(encoding="utf8"))))}

    def lock(self):
        return BuildLock(self.common / "codex-build.lock")


class BuildLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.stream = self.path.open("a+b")
        if self.stream.tell() == 0:
            self.stream.write(b" ")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise SourceError("Another agent is already building this repository") from exc
        return self

    def __exit__(self, *_args):
        self.stream.close()


def fingerprint(root, files):
    root = Path(root).resolve()
    values = {}
    for file in files:
        file = file.resolve()
        with file.open("rb") as stream:
            values[file.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return values


class Snapshot:
    def __init__(self, workspace, target, collector, metadata):
        self.workspace, self.collector = workspace, collector
        self.root, self.source = workspace.root, Path(target)
        self.files = fingerprint(self.root, collector(self.root))
        self.source.mkdir(parents=True)
        for name in self.files:
            target_file = self.source / name
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / name, target_file)
        if fingerprint(self.source, [self.source / name for name in self.files]) != self.files:
            raise SourceError("Source changed while freezing the build; no artifact is deliverable")
        digest = hashlib.sha256(json.dumps(self.files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.manifest = {"schema_version": 1, **metadata, "source_sha256": digest, "files": self.files}
        self.assert_unchanged()

    def assert_unchanged(self):
        if git(self.root, "rev-parse", "HEAD") != self.manifest["git_commit"]:
            raise SourceError("Source commit changed during the build; build again")
        if self.manifest.get("main_commit"):
            if git(self.root, "rev-parse", self.manifest["main_ref"]) != self.manifest["main_commit"]:
                raise SourceError("Shared main changed during the build; synchronize and build again")
        if fingerprint(self.root, self.collector(self.root)) != self.files:
            raise SourceError("Source files changed during the build; build again")

    def write_manifest(self, path, mode):
        value = {**self.manifest, "mode": mode}
        Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf8")
