"""
AuFarm — database.py

Async SQLite database access via aiosqlite.
Schema is initialized on first startup.
"""

import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "/data/aufarm.db")


async def get_db() -> aiosqlite.Connection:
    """Open a new database connection (row_factory set to dict-like Row)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL DEFAULT 0,
    service     TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS exploits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    filename    TEXT    NOT NULL,
    runtime     TEXT    NOT NULL DEFAULT 'python3',
    args        TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    timeout     INTEGER NOT NULL DEFAULT 30,
    target_mode TEXT    NOT NULL DEFAULT 'all',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS farm_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick        INTEGER NOT NULL,
    exploit_id  INTEGER NOT NULL,
    exploit_name TEXT   NOT NULL,
    target_id   INTEGER NOT NULL,
    target_host TEXT    NOT NULL,
    target_name TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    exit_code   INTEGER,
    duration_ms INTEGER,
    stdout      TEXT    NOT NULL DEFAULT '',
    stderr      TEXT    NOT NULL DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_results_tick         ON results (tick);
CREATE INDEX IF NOT EXISTS idx_results_exploit_id   ON results (exploit_id);
CREATE INDEX IF NOT EXISTS idx_results_target_id    ON results (target_id);
CREATE INDEX IF NOT EXISTS idx_results_status       ON results (status);
CREATE INDEX IF NOT EXISTS idx_results_created_at   ON results (created_at);

CREATE TABLE IF NOT EXISTS flags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flag         TEXT    NOT NULL UNIQUE,
    exploit_name TEXT    NOT NULL DEFAULT '',
    target_host  TEXT    NOT NULL DEFAULT '',
    target_name  TEXT    NOT NULL DEFAULT '',
    tick         INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'QUEUED',
    response     TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    submitted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_flags_status     ON flags (status);
CREATE INDEX IF NOT EXISTS idx_flags_created_at ON flags (created_at);

INSERT OR IGNORE INTO farm_config (key, value) VALUES
    ('tick_count',        '100'),
    ('tick_interval',     '60'),
    ('exploit_timeout',   '30'),
    ('concurrency',       '20'),
    ('current_tick',      '0'),
    ('farm_status',       'STOPPED'),
    ('continuous',        '0'),
    ('submit_enabled',    '0'),
    ('submit_mode',       'http_json'),
    ('submit_url',        ''),
    ('submit_token',      ''),
    ('submit_host',       ''),
    ('submit_port',       '31337'),
    ('flag_regex',        '[A-Za-z0-9_]{2,16}\\{[A-Za-z0-9_+=\\-/\\.]{8,}\\}'),
    ('submit_interval',   '5'),
    ('flag_lifetime',     '300');

"""


async def init_db() -> None:
    """Create the database schema if it doesn't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
