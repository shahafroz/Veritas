"""SQLite persistence (WAL mode). One connection per request; schema is idempotent."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    pii_redaction   INTEGER NOT NULL DEFAULT 1,
    monthly_budget_usd REAL NOT NULL DEFAULT 50.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES orgs(id),
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('viewer','editor','admin')),
    clearance     TEXT NOT NULL CHECK (clearance IN ('public','internal','confidential','restricted')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    org_id         INTEGER NOT NULL REFERENCES orgs(id),
    title          TEXT NOT NULL,
    filename       TEXT NOT NULL,
    classification TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    uploaded_by    INTEGER REFERENCES users(id),
    n_chunks       INTEGER NOT NULL DEFAULT 0,
    pii_redactions INTEGER NOT NULL DEFAULT 0,
    quarantined_chunks INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, sha256)
);
CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY,
    doc_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    org_id         INTEGER NOT NULL,
    ord            INTEGER NOT NULL,
    heading        TEXT NOT NULL,
    text           TEXT NOT NULL,
    classification TEXT NOT NULL,
    quarantined    INTEGER NOT NULL DEFAULT 0,
    embedder       TEXT,
    embedding      BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_org ON chunks(org_id);
CREATE TABLE IF NOT EXISTS queries (
    id            INTEGER PRIMARY KEY,
    org_id        INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    citations     TEXT NOT NULL,
    retrieved     TEXT NOT NULL,
    mode          TEXT NOT NULL,
    model         TEXT,
    latency_ms    INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    groundedness  REAL,
    abstained     INTEGER NOT NULL DEFAULT 0,
    flagged       TEXT,
    refusal_reason TEXT,
    trace         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_queries_org_time ON queries(org_id, created_at);
CREATE TABLE IF NOT EXISTS feedback (
    query_id   INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    comment    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (query_id, user_id)
);
-- Append-only, hash-chained audit trail. Each row commits to the previous row's hash,
-- so any edit or deletion breaks verification from that point on.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    org_id     INTEGER NOT NULL,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    resource   TEXT NOT NULL,
    detail     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_log(org_id, id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_write_lock = threading.Lock()
_db_path: Path | None = None


def init(path: Path) -> None:
    global _db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = path
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Additive migrations for databases created before a column existed.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(queries)")}
        for column in ("refusal_reason", "trace"):
            if column not in have:
                conn.execute(f"ALTER TABLE queries ADD COLUMN {column} TEXT")


def _open() -> sqlite3.Connection:
    assert _db_path is not None, "db.init() was not called"
    conn = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _open()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """Serialized writer: keeps the audit hash chain consistent under concurrency."""
    with _write_lock, connect() as conn:
        yield conn
