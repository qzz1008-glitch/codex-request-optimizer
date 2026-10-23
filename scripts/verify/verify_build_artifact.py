"""Check CI source identity and executable hash immediately before publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.build_inspector import PUBLIC_FILES, source_files
from scripts.build_source import SourceError, fingerprint, git

ROOT = Path(__file__).resolve().parents[2]


def verify(release, root=ROOT, tag=None):
    manifest = json.loads((release / "BUILD-SOURCE.json").read_text(encoding="utf8"))
    if manifest.get("mode") != "release" or manifest.get("dirty") is not False:
        raise SourceError("Only a clean release build can be published")
    if manifest.get("git_commit") != git(root, "rev-parse", "HEAD"):
        raise SourceError("Release artifact was built from a different commit")
    if tag is not None and tag != "v" + manifest.get("version", ""):
        raise SourceError("Release tag differs from the artifact version")
    files = fingerprint(root, source_files(root))
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if files != manifest.get("files") or digest != manifest.get("source_sha256"):
        raise SourceError("Release artifact omits current source changes")
    hashes = {}
    for line in (release / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        hashes[name] = digest
    public = release / "publish"
    if public.is_symlink() or not public.is_dir() or {path.name for path in public.iterdir()} != set(PUBLIC_FILES):
        raise SourceError("Public release must contain only the formal EXE, README and licenses")
    if any(path.is_symlink() or not path.is_file() for path in public.iterdir()):
        raise SourceError("Public release must contain regular files only")
    for name in ("CodexPacketInspector.exe", "BUILD-SOURCE.json", *("publish/" + name for name in PUBLIC_FILES)):
        with (release / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if hashes.get(name) != digest:
            raise SourceError("Release artifact hash mismatch: " + name)
    if hashes["publish/CodexPacketInspector.exe"] != hashes["CodexPacketInspector.exe"]:
        raise SourceError("Public EXE differs from the verified formal EXE")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    try:
        verify(args.release_dir, tag=args.tag)
    except (OSError, ValueError) as exc:
        print(json.dumps({"verified": False, "error": str(exc)}))
        return 1
    print(json.dumps({"verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
