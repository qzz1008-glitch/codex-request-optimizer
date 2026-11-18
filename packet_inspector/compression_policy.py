"""Durable automatic zstd phases; recover from packet metadata, never bodies."""
import json
import time
from typing import NamedTuple

from .request_context import temporary_kind_from_metadata
from .request_types import image_request_kind
from .transport import AUTO_ZSTD_HIGH_LEVEL, below_auto_zstd_threshold

INITIAL_HIGH_BYTES = 1_500_000
ORIGINAL, FAST, HIGH = 0, 1, 17
# Phase IDs are durable state, not encoder levels. Retain the existing HIGH ID
# so upgraded and draining workers can share the database without migration.


def select_phase(previous, uncompressed_bytes, budget_bytes):
    """One selection rule for both main requests and read-only auxiliaries.

    Small plaintext -> original; unknown large plaintext -> initial size split;
    known high -> high; known original/fast -> fast. Wire size can only promote
    the *next* request in finish(), never change the current selection.
    """
    if below_auto_zstd_threshold(uncompressed_bytes, budget_bytes):
        return ORIGINAL
    if previous is None:
        return HIGH if uncompressed_bytes >= INITIAL_HIGH_BYTES else FAST
    return HIGH if previous == HIGH else FAST


def phase_level(phase):
    return AUTO_ZSTD_HIGH_LEVEL if phase == HIGH else FAST


def is_main_request(meta):
    # A subagent has its own real session. App-generated auxiliary dispatches
    # must not look like a compacted main request just because they are small.
    return (not meta.get("is_compaction")
            and meta.get("request_kind") not in {"prewarm", "memory", "tool_search", "image_generation", "image_edit"}
            and image_request_kind(meta.get("path", "")) is None
            and temporary_kind_from_metadata(meta) in {None, "subagent"})


def _rewritten_body_is_small(size, recorded_budget, budget_bytes):
    if type(recorded_budget) is int and recorded_budget > 0:
        budget_bytes = recorded_budget
    return type(size) is int and size >= 0 and below_auto_zstd_threshold(size, budget_bytes)


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
            c.execute("""CREATE INDEX IF NOT EXISTS packet_summary_image_zstd_ticket
                ON packet_summaries(json_extract(meta,'$.relay_auto_zstd_ticket'))
                WHERE json_type(meta,'$.image_isolation_decoded_after')='integer'""")

    @staticmethod
    def _record_phase(c, key, before_ticket, budget_bytes):
        # Both retained summaries and full-detail records live in this metadata
        # index. No age cutoff and no dependency on the request body/details.
        rows = c.execute("""SELECT meta FROM packet_summaries
            WHERE thread=? AND method='POST' AND kind='generation'
            AND json_extract(meta,'$.source')='live'
            AND json_extract(meta,'$.relay_compression_scope')=?
            AND coalesce(json_extract(meta,'$.relay_auto_zstd_ticket'),0)<?
            ORDER BY created DESC,id DESC""", (key[1], key[0], before_ticket))
        for row in rows:
            meta = json.loads(row[0])
            if (not is_main_request(meta)
                    or not meta.get("path", "").split("?", 1)[0].rstrip("/").endswith("/responses")):
                continue
            rewritten = meta.get("image_isolation_decoded_after")
            small_rewrite = _rewritten_body_is_small(rewritten, meta.get("relay_auto_zstd_budget_bytes"), budget_bytes)
            phase = meta.get("relay_auto_zstd_next_phase")
            if type(phase) is int and phase in {ORIGINAL, FAST, HIGH}:
                return ORIGINAL if small_rewrite else phase
            # Older records lack a phase. Preserve a known high level even
            # when its compressed packet is small; pressure also promotes high.
            size = next((meta.get(k) for k in ("overflow_original_wire_bytes", "usage_override_request_bytes",
                                              "outbound_wire_bytes", "relay_wire_bytes")
                         if type(meta.get(k)) is int and meta[k] >= 0), None)
            if size is None:
                continue
            if small_rewrite:
                return ORIGINAL
            summary = meta.get("overflow_compaction_calls", 0) > 0 or meta.get("overflow_cache_hit") is True
            level = (meta.get("transport_zstd_level") if "transport_content_encoding" in meta
                     else None if summary else meta.get("relay_zstd_level"))
            if level in {AUTO_ZSTD_HIGH_LEVEL, 17} or not below_auto_zstd_threshold(size, budget_bytes):
                return HIGH
            uncompressed = rewritten if type(rewritten) is int and rewritten >= 0 else meta.get("decoded_bytes")
            return (ORIGINAL if type(uncompressed) is int and below_auto_zstd_threshold(uncompressed, budget_bytes)
                    else FAST)
        return None

    def _previous_phase(self, c, key, stored, before_ticket, budget_bytes):
        if stored is None:
            return self._record_phase(c, key, before_ticket, budget_bytes)
        phase, ticket = stored
        if phase != ORIGINAL:
            # Older workers advanced the phase using the body before image isolation.
            # Only repair a stored phase using the record for that exact ticket.
            row = c.execute("""SELECT json_extract(meta,'$.image_isolation_decoded_after'),
                       json_extract(meta,'$.relay_auto_zstd_budget_bytes')
                FROM packet_summaries
                WHERE json_extract(meta,'$.relay_auto_zstd_ticket')=? AND thread=?
                AND json_extract(meta,'$.source')='live'
                AND json_extract(meta,'$.relay_compression_scope')=?
                AND json_type(meta,'$.image_isolation_decoded_after')='integer'""",
                (ticket, key[1], key[0])).fetchone()
            if row and _rewritten_body_is_small(row[0], row[1], budget_bytes):
                return ORIGINAL
        return phase

    def reserve(self):
        # Allocate before inspection/compression so slow old requests cannot
        # overwrite a later request, including across draining relay workers.
        with self.store.lock, self.store.connect() as c:
            c.execute("UPDATE auto_zstd_clock SET serial=serial+1 WHERE singleton=1")
            return c.execute("SELECT serial FROM auto_zstd_clock WHERE singleton=1").fetchone()[0]

    def level(self, key, uncompressed_bytes, budget_bytes):
        # Auxiliary dispatches can use the main session's selected level,
        # but must not advance its ticket or reset it with their shorter body.
        previous = None
        if key is not None:
            with self.store.connect() as c:
                row = c.execute("SELECT phase,ticket FROM auto_zstd_sessions WHERE scope=? AND thread=?", key).fetchone()
                before = c.execute("SELECT serial FROM auto_zstd_clock WHERE singleton=1").fetchone()[0] + 1
                previous = self._previous_phase(c, key, row, before, budget_bytes)
        return phase_level(select_phase(previous, uncompressed_bytes, budget_bytes))

    def begin(self, key, ticket, uncompressed_bytes, budget_bytes):
        with self.store.lock, self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT phase,ticket FROM auto_zstd_sessions WHERE scope=? AND thread=?", key).fetchone()
            previous_phase = self._previous_phase(c, key, previous, ticket, budget_bytes)
            phase = select_phase(previous_phase, uncompressed_bytes, budget_bytes)
            decision = CompressionDecision(ticket, phase, previous_phase)
            reset = c.execute("SELECT reset_after FROM auto_zstd_clock WHERE singleton=1").fetchone()[0]
            if ticket <= reset or (previous and ticket <= previous[1]):
                return decision
            now = time.time()
            c.execute("""INSERT INTO auto_zstd_sessions VALUES(?,?,?,?,?)
                ON CONFLICT(scope,thread) DO UPDATE SET
                phase=excluded.phase,ticket=excluded.ticket,touched=excluded.touched""", (*key, phase, ticket, now))
            return decision

    def finish(self, key, decision, compressed_bytes, budget_bytes):
        phase = decision.phase
        if phase == FAST and not below_auto_zstd_threshold(compressed_bytes, budget_bytes):
            phase = HIGH
        with self.store.lock, self.store.connect() as c:
            changed = c.execute("UPDATE auto_zstd_sessions SET phase=? WHERE scope=? AND thread=? AND ticket=?",
                                (phase, *key, decision.ticket)).rowcount
            return phase if changed else None

    def clear(self):
        # Fence in-flight updates on a mode switch. Stored packet metadata stays
        # valid: a later request may recover its phase from the latest record.
        with self.store.lock, self.store.connect() as c:
            c.execute("UPDATE auto_zstd_clock SET reset_after=serial WHERE singleton=1")
            c.execute("DELETE FROM auto_zstd_sessions")
