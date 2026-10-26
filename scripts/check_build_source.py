"""Publish main and release tags only from the integrated main commit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from scripts.build_source import SourceError, Workspace, git

ROOT = Path(__file__).resolve().parents[1]


def github_json(resource):
    executable = shutil.which("gh")
    if not executable and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "Programs/GitHub CLI/bin/gh.exe"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise SourceError("GitHub CLI is required to verify an unpublished release retry")
    try:
        result = subprocess.run([executable, "api", "--hostname", "github.com", "--include", resource],
                                capture_output=True, encoding="utf8", errors="replace", timeout=40)
        headers, separator, body = result.stdout.partition("\n\n")
        status = re.match(r"HTTP/\S+\s+(\d{3})\b", headers)
        if not separator or not status:
            raise ValueError("Missing GitHub response status")
        code = int(status.group(1))
        if result.returncode and code != 404:
            raise ValueError("GitHub request did not complete successfully")
        return code, json.loads(body)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SourceError("Could not verify the unpublished release state") from exc


def retry_unpublished_tag(root, ref, remote_sha, commit):
    tag = ref.removeprefix("refs/tags/")
    opt_in = git(root, "config", "--get", "codex.retryUnpublishedRelease", check=False)
    if opt_in.returncode or opt_in.stdout.strip() != f"{tag}:{remote_sha}":
        raise SourceError("Do not replace a published version tag")
    remote = git(root, "remote", "get-url", "origin")
    repository = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?", remote)
    if not repository:
        raise SourceError("An unpublished release retry requires a verified GitHub origin")
    old_commit = git(root, "rev-parse", remote_sha + "^{commit}")
    if git(root, "merge-base", "--is-ancestor", old_commit, commit, check=False).returncode:
        raise SourceError("Retried release source must retain the failed attempt's history")
    prefix = "repos/" + repository.group(1)
    status, release = github_json(prefix + "/releases/tags/" + tag)
    if status != 404 or release.get("message") != "Not Found":
        raise SourceError("Do not replace a tag with an existing or unverified GitHub release")
    status, data = github_json(prefix + "/actions/runs?head_sha=" + old_commit + "&per_page=100")
    runs = [run for run in data.get("workflow_runs", []) if run.get("head_branch") == tag]
    if status != 200 or not runs or any(run.get("status") != "completed"
            or run.get("conclusion") not in {"failure", "cancelled", "timed_out"} for run in runs):
        raise SourceError("Only a completed failed release attempt can reuse its unpublished tag")
    git(root, "update-ref", "refs/archive/unpublished-release-tags/" + tag + "/" + remote_sha, remote_sha)


def check_push(root, updates):
    protected = []
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise SourceError("Invalid pre-push update")
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if set(local_sha) == {"0"}:
            continue
        if remote_ref == "refs/heads/main" or remote_ref.startswith("refs/tags/v"):
            protected.append((local_sha, remote_ref, remote_sha))
    if not protected:
        return
    workspace = Workspace(root)
    with workspace.lock():
        metadata = workspace.validate("release")
        for sha, ref, remote_sha in protected:
            commit = git(root, "rev-parse", sha + "^{commit}")
            if commit != metadata["git_commit"]:
                raise SourceError("Publish only the current shared main commit; merge the completed task branch first")
            if ref.startswith("refs/tags/") and ref != "refs/tags/v" + metadata["version"]:
                raise SourceError("Release tag must match the shared source VERSION")
            if set(remote_sha) != {"0"}:
                if ref == "refs/heads/main":
                    if git(root, "merge-base", "--is-ancestor", remote_sha, commit, check=False).returncode:
                        raise SourceError("Refusing a non-fast-forward main update; fetch and integrate remote main first")
                elif sha != remote_sha:
                    retry_unpublished_tag(root, ref, remote_sha, commit)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-push", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.pre_push:
            check_push(ROOT, sys.stdin.read())
        else:
            workspace = Workspace(ROOT)
            with workspace.lock():
                workspace.validate("dev")
        return 0
    except SourceError as exc:
        print(json.dumps({"source": "refused", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
