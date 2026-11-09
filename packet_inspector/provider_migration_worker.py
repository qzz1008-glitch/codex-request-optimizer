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


def migrate(home, backup_dir, aliases, target):
    home, backup_dir = Path(home), Path(backup_dir)
    aliases = sorted(set(aliases) - {target})
    result = {"changed": 0, "databases": 0, "backups": [], "warnings": []}
    if not aliases:
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
                # Re-read under the write transaction; concurrent native writes
                # either finish before this snapshot or wait until commit.
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    rows = db.execute(select, aliases).fetchall()
                    if not rows:
                        continue
                    payload = json.dumps({"database": str(path), "target": target, "previous": rows},
                                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256(payload).hexdigest()[:24]
                    backup = backup_dir / f"provider-migration-{digest}.json"
                    # Complete the recovery file before changing any field.
                    if backup.exists():
                        if backup.read_bytes() != payload:
                            raise ValueError("provider recovery snapshot does not match")
                    else:
                        descriptor, staging = tempfile.mkstemp(prefix=".provider-migration-", dir=backup_dir)
                        try:
                            with os.fdopen(descriptor, "wb") as stream:
                                stream.write(payload)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(staging, backup)
                        finally:
                            Path(staging).unlink(missing_ok=True)
                    db.executemany("UPDATE threads SET model_provider=? WHERE id=? AND model_provider=?",
                                   ((target, thread, before) for thread, before in rows))
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
    return result


if __name__ == "__main__":
    import sys
    request = json.load(sys.stdin)
    print("CPI_PROVIDER_MIGRATION=" + json.dumps(migrate(**request), ensure_ascii=False))
