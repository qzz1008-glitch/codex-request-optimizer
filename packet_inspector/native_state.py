"""Read-only discovery of Codex's versioned state indexes."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


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
