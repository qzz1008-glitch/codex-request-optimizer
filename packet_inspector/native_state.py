"""Read-only discovery of Codex's versioned state indexes."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3


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


def thread_origins(home, ids):
    """Resolve the saved host for an exact Codex session ID."""
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
    for thread in ids:
        if not thread:
            continue
        host_id = memberships.get(thread)
        if not isinstance(host_id, str) or not host_id:
            assignment = assignments.get(thread)
            host_id = assignment.get("hostId") if isinstance(assignment, dict) else None
        if isinstance(host_id, str) and host_id:
            # An explicit host ID takes precedence even when the saved host was removed.
            origins[thread] = hosts.get(host_id)
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
