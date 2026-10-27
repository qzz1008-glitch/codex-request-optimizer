"""Durable automatic zstd phases, independent of captured request retention."""
import time
from typing import NamedTuple

from .request_context import temporary_kind_from_metadata

INITIAL_HIGH_BYTES = 1_500_000
ORIGINAL, FAST, HIGH = 0, 1, 17


def select_phase(previous, original_bytes, budget_bytes):
    """One selection rule for both main requests and read-only auxiliaries.

    Small original -> original; unknown large original -> initial size split;
    known high -> high; known original/fast -> fast. Wire size can only promote
    the *next* request in finish(), never change the current selection.
    """
    if original_bytes * 100 < budget_bytes * 90:
        return ORIGINAL
    if previous is None:
        return HIGH if original_bytes >= INITIAL_HIGH_BYTES else FAST
    return HIGH if previous == HIGH else FAST


def phase_level(phase):
    return HIGH if phase == HIGH else FAST


def is_main_request(meta):
    # A subagent has its own real session. App-generated auxiliary dispatches
    # must not look like a compacted main request just because they are small.
    return (not meta.get("is_compaction")
            and meta.get("request_kind") not in {"prewarm", "memory", "tool_search"}
            and temporary_kind_from_metadata(meta) in {None, "subagent"})


class CompressionDecision(NamedTuple):
    ticket: int
    phase: int  # 0 = original body, 1 = fast, 17 = locked high compression
    previous_phase: int | None

    @property
    def level(self):
        return phase_level(self.phase)


class CompressionPolicy:
    def __init__(self, store):
        self.store = store
        with store.lock, store.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS auto_zstd_clock (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                serial INTEGER NOT NULL, reset_after INTEGER NOT NULL)""")
            c.execute("INSERT OR IGNORE INTO auto_zstd_clock VALUES(1,0,0)")
            c.execute("""CREATE TABLE IF NOT EXISTS auto_zstd_sessions (
                scope TEXT NOT NULL, thread TEXT NOT NULL,
                phase INTEGER NOT NULL CHECK(phase IN (0,1,17)),
                ticket INTEGER NOT NULL, touched REAL NOT NULL,
                PRIMARY KEY(scope,thread))""")
            c.execute("CREATE INDEX IF NOT EXISTS auto_zstd_unlocked ON auto_zstd_sessions(touched) WHERE phase<>17")

    def reserve(self):
        # Allocate before inspection/compression so slow old requests cannot
        # overwrite a later request, including across draining relay workers.
        with self.store.lock, self.store.connect() as c:
            c.execute("UPDATE auto_zstd_clock SET serial=serial+1 WHERE singleton=1")
            return c.execute("SELECT serial FROM auto_zstd_clock WHERE singleton=1").fetchone()[0]

    def level(self, key, original_bytes, budget_bytes):
        # Auxiliary dispatches can use the main session's selected level,
        # but must not advance its ticket or reset it with their shorter body.
        previous = None
        if key is not None:
            with self.store.connect() as c:
                row = c.execute("SELECT phase FROM auto_zstd_sessions WHERE scope=? AND thread=?", key).fetchone()
                previous = row[0] if row else None
        return phase_level(select_phase(previous, original_bytes, budget_bytes))

    def begin(self, key, ticket, original_bytes, budget_bytes):
        with self.store.lock, self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT phase,ticket FROM auto_zstd_sessions WHERE scope=? AND thread=?", key).fetchone()
            phase = select_phase(previous[0] if previous else None, original_bytes, budget_bytes)
            decision = CompressionDecision(ticket, phase, previous[0] if previous else None)
            reset = c.execute("SELECT reset_after FROM auto_zstd_clock WHERE singleton=1").fetchone()[0]
            if ticket <= reset or (previous and ticket <= previous[1]):
                return decision
            now = time.time()
            c.execute("""INSERT INTO auto_zstd_sessions VALUES(?,?,?,?,?)
                ON CONFLICT(scope,thread) DO UPDATE SET
                phase=excluded.phase,ticket=excluded.ticket,touched=excluded.touched""", (*key, phase, ticket, now))
            # Only inactive unlocked rows expire. A locked phase survives time,
            # capture disablement and packet cleanup until a real small request.
            c.execute("DELETE FROM auto_zstd_sessions WHERE phase<>17 AND touched<?", (now - 86400,))
            return decision

    def finish(self, key, decision, compressed_bytes, budget_bytes):
        phase = decision.phase
        if phase == FAST and compressed_bytes * 100 >= budget_bytes * 90:
            phase = HIGH
        with self.store.lock, self.store.connect() as c:
            changed = c.execute("UPDATE auto_zstd_sessions SET phase=? WHERE scope=? AND thread=? AND ticket=?",
                                (phase, *key, decision.ticket)).rowcount
            return phase if changed else None

    def clear(self):
        # Explicitly switching automatic mode off/on starts a new policy epoch.
        # Tickets reserved before that switch must not recreate old state.
        with self.store.lock, self.store.connect() as c:
            c.execute("UPDATE auto_zstd_clock SET reset_after=serial WHERE singleton=1")
            c.execute("DELETE FROM auto_zstd_sessions")
