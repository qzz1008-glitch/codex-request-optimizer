"""Check CI source identity and executable hash immediately before publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

from scripts.build_inspector import LINUX_FILES, PUBLIC_FILES, source_files
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
    linux = str(manifest.get("platform", "")).startswith("linux")
    if linux and tag is not None:
        raise SourceError("Linux validation packages are not part of the Windows public release channel")
    public = release / ("linux-package" if linux else "publish")
    public_files = LINUX_FILES if linux else PUBLIC_FILES
    executable = public_files[0]
    if public.is_symlink() or not public.is_dir() or {path.name for path in public.iterdir()} != set(public_files):
        raise SourceError("Public release must contain only the formal EXE, README and licenses")
    if any(path.is_symlink() or not path.is_file() for path in public.iterdir()):
        raise SourceError("Public release must contain regular files only")
    names = [executable, "BUILD-SOURCE.json", *(public.name + "/" + name for name in public_files)]
    if linux:
        architecture = str(manifest.get("architecture", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]+", architecture):
            raise SourceError("Linux package architecture is invalid")
        names.append("CodexPacketInspector-linux-" + architecture + ".tar.gz")
    for name in names:
        with (release / name).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if hashes.get(name) != digest:
            raise SourceError("Release artifact hash mismatch: " + name)
    if hashes[public.name + "/" + executable] != hashes[executable]:
        raise SourceError("Public EXE differs from the verified formal EXE")
    if linux:
        with tarfile.open(release / names[-1], "r:gz") as archive:
            members = archive.getmembers()
            expected = {"codex-packet-inspector/" + name for name in LINUX_FILES}
            if len(members) != len(expected) or {item.name for item in members} != expected:
                raise SourceError("Linux archive contains unexpected or duplicate files")
            for item in members:
                if not item.isfile() or item.mode != (0o755 if item.name.endswith("/" + executable) else 0o644):
                    raise SourceError("Linux archive contains unsafe file types or permissions")
                with archive.extractfile(item) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if hashes[public.name + "/" + item.name.split("/", 1)[1]] != digest:
                    raise SourceError("Linux archive differs from the verified package")


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
