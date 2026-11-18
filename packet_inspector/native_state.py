"""Read-only discovery of Codex's versioned state indexes."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3


WSL_HOST_ID = "remote-ssh-discovered:wsl-local"


def read_global_state(home):
    limit = 16 * 1024 * 1024
    try:
        with (Path(home) / ".codex-global-state.json").open("rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > limit:
        raise ValueError("Codex global state is too large")
    state = json.loads(raw.decode("utf-8-sig"))
    return state if isinstance(state, dict) else {}


def managed_remote_connections(state):
    rows = state.get("codex-managed-remote-connections")
    if isinstance(rows, dict):
        return rows.values()
    return rows if isinstance(rows, list) else ()


def thread_origins(home):
    """Return the native host ID and display metadata keyed by session ID."""
    try:
        state = read_global_state(home)
    except (OSError, ValueError):
        return {}
    memberships = state.get("thread-project-membership-host-ids")
    assignments = state.get("thread-project-assignments")
    memberships = memberships if isinstance(memberships, dict) else {}
    assignments = assignments if isinstance(assignments, dict) else {}

    hosts = {"local": {"kind": "local"}}
    for row in managed_remote_connections(state):
        if not isinstance(row, dict) or not isinstance(row.get("hostId"), str):
            continue
        for value in (row.get("hostname"), row.get("alias")):
            if isinstance(value, str):
                address = value.strip().rsplit("@", 1)[-1].removeprefix("[").removesuffix("]")
                if re.fullmatch(r"[\w.:%-]{1,255}", address):
                    hosts.setdefault(row["hostId"], {"kind": "remote", "address": address})
                    break

    origins = {}
    for thread in memberships.keys() | assignments.keys():
        host_id = memberships.get(thread)
        if not isinstance(host_id, str) or not host_id:
            assignment = assignments.get(thread)
            host_id = assignment.get("hostId") if isinstance(assignment, dict) else None
        if isinstance(host_id, str) and host_id:
            # An explicit host ID takes precedence even when the saved host was removed.
            origins[thread] = (host_id, hosts.get(host_id))
    return origins


def state_databases(home):
    """Newest schema first; older indexes may still own unmigrated threads."""
    def revision(path):
        suffix = path.stem.removeprefix("state_")
        return (int(suffix) if suffix.isdigit() else -1, path.name)

    return sorted(Path(home).glob("state_*.sqlite"), key=revision, reverse=True)


@contextmanager
def read_state(database, *, timeout=.2):
    # mode=ro prevents a disappearing index from being recreated by this reader.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)) as db:
        db.execute("PRAGMA query_only=ON")
        yield db


# Executed inside Linux for both WSL and SSH. Only requested session metadata
# leaves the host. SQLite continues to own its native WAL and locks.
LABEL_READER = r'''
def read_label_data(home, ids):
    import json, sqlite3
    from contextlib import closing
    from pathlib import Path

    home = Path(home).resolve()
    ids = set(ids[:300])
    wanted = ids.copy()
    rows, edges, index = [], [], {}

    def revision(path):
        value = path.stem[len("state_"):]
        return (int(value) if value.isdigit() else -1, path.name)

    for path in sorted(home.glob("state_*.sqlite"), key=revision, reverse=True):
        if not wanted:
            break
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.execute("PRAGMA query_only=ON")
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if "id" not in columns:
                    continue
                times = ["nullif(updated_at_ms,0)/1000.0"] if "updated_at_ms" in columns else []
                times += ["updated_at"] if "updated_at" in columns else []
                updated = "coalesce(" + ",".join(times + ["NULL"]) + ")" if times else "NULL"
                fields = ["id"] + ["substr(" + key + ",1," + str(size) + ")" if key in columns else "NULL"
                    for key, size in (("title", 512), ("name", 512), ("agent_nickname", 128), ("source", 8192))] + [updated]
                pending = sorted(wanted)
                placeholders = ",".join("?" for _ in pending)
                current = db.execute("SELECT " + ",".join(fields) + " FROM threads WHERE id IN (" +
                                     placeholders + ")", pending).fetchall()
                try:
                    edges += db.execute("SELECT child_thread_id,min(parent_thread_id) FROM thread_spawn_edges "
                        "WHERE child_thread_id IN (" + placeholders +
                        ") GROUP BY child_thread_id HAVING count(DISTINCT parent_thread_id)=1", pending).fetchall()
                except sqlite3.OperationalError:
                    pass
                rows += current
                wanted.difference_update(row[0] for row in current)
        except (OSError, sqlite3.Error):
            continue
    try:
        with (home / "session_index.jsonl").open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 1024 * 1024)
            stream.seek(offset)
            lines = stream.read(1024 * 1024).split(b"\n")
        for line in reversed(lines[1:-1] if offset else lines[:-1]):
            try:
                item = json.loads(line)
                key, title = item.get("id"), item.get("thread_name")
                if key in ids and key not in index and isinstance(title, str) and title.strip():
                    index[key] = title[:512]
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return {"rows": rows, "edges": edges, "index": index}
'''
