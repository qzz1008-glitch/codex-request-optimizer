"""Migrate provider configuration in native indexes, never message histories.

This stdlib-only worker also runs inside WSL so SQLite uses native WAL locks.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def config_revision(home, config_path):
    """Latest native configuration write in this migration's scope."""
    return max((path.stat().st_mtime_ns for path in
                (config_path, *home.glob("*.config.toml")) if path.is_file()), default=0)


def linux_writer_before(home, revision, *, proc=Path("/proc")):
    """Find Codex processes in this account/config scope that can hold old sessions."""
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise OSError("Codex process lifetime is unavailable")
    boot = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    uid = os.getuid()
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            name = (entry / "comm").read_text().strip()
            if name != "codex" and not name.startswith("codex-"):
                continue
            # The proc start time has tick precision. Treat a boundary tick as old.
            start_ticks = int((entry / "stat").read_text().rsplit(") ", 1)[1].split()[19])
            started = boot + start_ticks * 1_000_000_000 // ticks_per_second
            if started > revision:
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if not args[0]:  # Exited or zombie.
                continue
            if (b"app-server", b"proxy") in zip(args, args[1:]) or (b"app-server", b"daemon") in zip(args, args[1:]):
                continue  # The proxy or manager is not the native session writer.
            environment = dict(item.split(b"=", 1) for item in (entry / "environ").read_bytes().split(b"\0")
                               if item.startswith((b"CODEX_HOME=", b"HOME=")))
            writer_home = environment.get(b"CODEX_HOME")
            if not writer_home and not environment.get(b"HOME"):
                raise OSError("Codex process configuration directory is unavailable")
            scope = Path(os.fsdecode(writer_home)) if writer_home else Path(os.fsdecode(environment[b"HOME"])) / ".codex"
            if not scope.is_absolute():
                scope = (entry / "cwd").resolve() / scope
            if scope.resolve() == home:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue  # The process has stopped.
    return False


def write_recovery(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix=".provider-migration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)


def migrate(home, backup_dir, aliases, target, *, writer_before=None, require_reload=False, config_path=None):
    home, backup_dir = Path(home), Path(backup_dir)
    config_path = Path(config_path) if config_path is not None else home / "config.toml"
    aliases = sorted(set(aliases) - {target})
    result = {"changed": 0, "databases": 0, "backups": [], "warnings": []}
    if not aliases:
        return result
    pending = None
    if writer_before is not None:
        scope = hashlib.sha256(os.fsencode(home.resolve())).hexdigest()[:24]
        pending = backup_dir / f"pending-{scope}"
    was_waiting = pending is not None and pending.is_file()
    reload_checked = False

    def verify_reload():
        nonlocal reload_checked
        if writer_before is not None and not reload_checked:
            try:
                previous_revision = int(pending.read_text(encoding="ascii")) if was_waiting else 0
                # An unrelated save by the new desktop process must not move
                # the checkpoint past the restart that was already observed.
                revision = previous_revision
                if require_reload or not was_waiting:
                    revision = max(revision, config_revision(home, config_path))
                if not revision:
                    raise ValueError("configuration revision is unavailable")
                if writer_before(home.resolve(), revision):
                    if not was_waiting or revision != previous_revision:
                        write_recovery(pending, str(revision).encode("ascii"))
                    result["waiting_for_reload"] = True
                    return False
            except (OSError, ValueError, IndexError) as exc:
                result["waiting_for_reload"] = True
                result["warnings"].append({"file": "config.toml", "reason": "session_provider_migration_failed",
                                           "detail": type(exc).__name__ + ": " + str(exc)[:240]})
                return False
            reload_checked = True
        return True

    if (require_reload or was_waiting) and not verify_reload():
        return result
    placeholders = ",".join("?" for _ in aliases)
    for path in sorted(home.glob("state_*.sqlite")):
        try:
            # mode=rw cannot recreate a disappearing native index.
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if not {"id", "model_provider"} <= columns:
                    raise ValueError("unsupported threads provider schema")
                select = f"SELECT id,model_provider FROM threads WHERE model_provider IN ({placeholders}) ORDER BY id"
                if db.execute(select, aliases).fetchone() is None:
                    continue
                if not verify_reload():
                    return result
                # Re-read under the write transaction; concurrent native writes
                # either finish before this snapshot or wait until commit.
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    rows = db.execute(select, aliases).fetchall()
                    if not rows:
                        continue
                    payload = json.dumps({"database": str(path), "target": target, "previous": rows},
                                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    digest = hashlib.sha256(payload).hexdigest()[:24]
                    backup = backup_dir / f"provider-migration-{digest}.json"
                    # Complete the recovery file before changing any field.
                    if backup.exists():
                        if backup.read_bytes() != payload:
                            raise ValueError("provider recovery snapshot does not match")
                    else:
                        write_recovery(backup, payload)
                    db.executemany("UPDATE threads SET model_provider=? WHERE id=? AND model_provider=?",
                                   ((target, thread, before) for thread, before in rows))
                    if db.execute(select, aliases).fetchone() is not None:
                        raise ValueError("legacy session provider remains after migration")
                result["changed"] += len(rows)
                result["databases"] += 1
                result["backups"].append(str(backup))
        except (OSError, ValueError, sqlite3.Error) as exc:
            result["warnings"].append({"file": path.name, "reason": "session_provider_migration_failed",
                                       "detail": type(exc).__name__ + ": " + str(exc)[:240]})
    # Recovery metadata is small, contains no messages or credentials, and does
    # not grow on unchanged checks. Keep the latest 32 successful snapshots.
    if result["changed"]:
        try:
            snapshots = sorted(backup_dir.glob("provider-migration-*.json"), key=lambda p: p.stat().st_mtime_ns,
                               reverse=True)
        except OSError:
            return result  # Cleanup must not discard a committed migration result.
        for backup in snapshots[32:]:
            try:
                if backup.resolve().parent == backup_dir.resolve():
                    backup.unlink()
            except OSError:
                pass  # A recovery copy may be open; never fail a committed migration.
    if pending is not None and not result["warnings"]:
        pending.unlink(missing_ok=True)
        if was_waiting:
            result["verified_after_reload"] = True
    return result


if __name__ == "__main__":
    import sys
    request = json.load(sys.stdin)
    print("CPI_PROVIDER_MIGRATION=" + json.dumps(migrate(**request, writer_before=linux_writer_before),
                                                 ensure_ascii=False))
