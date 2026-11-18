"""SQLite connection management and schema.

Concurrency model
-----------------
* The database runs in WAL mode so readers (queries) never block a writer
  (batch submission) and vice versa.
* Every write batch runs inside a single ``BEGIN IMMEDIATE`` transaction.
  Either the whole batch is committed or nothing changes ("failure keeps the
  old chain").  Readers always see a fully committed snapshot, never a mix of
  revisions -- including after a process restart, because uncommitted data is
  never persisted.
* :func:`read_snapshot` pins one committed snapshot across several reads
  (exposure reports and their change comparisons), so interleaved writes can
  never contribute some old and some new revisions to one result.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = {busy_timeout};

CREATE TABLE IF NOT EXISTS waybills (
    id              TEXT PRIMARY KEY,
    origin_party    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- One row per (scan_id, revision).  A scan's adopted state is the row with
-- the highest revision for that scan_id; withdrawn rows are tombstones.
CREATE TABLE IF NOT EXISTS scans (
    waybill_id      TEXT NOT NULL REFERENCES waybills(id),
    scan_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision > 0),
    event_type      TEXT NOT NULL CHECK (event_type IN ('HANDOVER', 'WITHDRAWAL')),
    ts_utc          TEXT,
    giver           TEXT,
    receiver        TEXT,
    payload_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (waybill_id, scan_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_scans_waybill ON scans (waybill_id, scan_id, revision);

-- Exposure reports are immutable snapshots: the conclusion, its basis and the
-- exact set of waybills/scans it was computed from are stored as JSON and
-- never recomputed.  A report row and its detail row are committed together.
CREATE TABLE IF NOT EXISTS exposure_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    party           TEXT NOT NULL,
    at_utc          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    waybill_count   INTEGER NOT NULL,
    at_party_count  INTEGER NOT NULL,
    not_at_party_count INTEGER NOT NULL,
    undeterminable_count INTEGER NOT NULL,
    payload         TEXT NOT NULL
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sane, chain-integrity-preserving pragmas."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # we manage transactions explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema (idempotent)."""
    conn = connect(db_path)
    try:
        conn.executescript(
            _SCHEMA.format(busy_timeout=config.BUSY_TIMEOUT_MS)
        )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize writers with BEGIN IMMEDIATE.

    Any exception rolls the whole batch back, leaving the previously
    committed chain untouched.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Hold one consistent committed snapshot for a sequence of reads.

    SQLite in WAL mode pins the snapshot at the first ``SELECT`` inside a
    read transaction, so writers that commit while we are reading are simply
    invisible until the next snapshot.  This is what lets an exposure report
    or its change comparison use one fixed waybill set and one fixed set of
    scan revisions even when backfills/corrections/withdrawals interleave.
    """
    conn.execute("BEGIN;")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        # Read-only: committing releases the snapshot without changing data.
        conn.commit()
