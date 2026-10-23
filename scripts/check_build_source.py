"""Publish main and release tags only from the integrated main commit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scripts.build_source import SourceError, Workspace, git

ROOT = Path(__file__).resolve().parents[1]


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
                    raise SourceError("Do not replace a published version tag")


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
