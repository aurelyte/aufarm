"""
AuFarm — database.py

Async SQLite database access via aiosqlite.
Schema is initialized on first startup.
"""

import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "/data/aufarm.db")

OPPONENTS = {
    "10.13.37.1": "Maaf kami belum sempat diskusi namanya karena sibuk",
    "10.13.37.2": "HCS Ambarawa Express",
    "10.13.37.3": "HCS infinits park reborn",
    "10.13.37.4": "nama tim kita ini aja",
    "10.13.37.5": "Coba coba",
    "10.13.37.6": "Sembarang Wes",
    "10.13.37.7": "Fokus KKN ngapain ctf gak dapet apa apa",
    "10.13.37.8": "Liburan DiRumah",
    "10.13.37.9": "PETIR mas aska butuh tiket karna dari singapura",
    "10.13.37.10": "PETIR Pengen Main Maimai Cuman Ga Ada Credit",
    "10.13.37.11": "DOSCOM Zero Day Scholars",
    "10.13.37.12": "404 Rest Not Found",
    "10.13.37.13": "ctfhoyahoya",
    "10.13.37.14": "hmcc apaya",
    "10.13.37.16": "Baru Kenal",
    "10.13.37.17": "CSUI frontshots",
    "10.13.37.18": "Pengen gemastik",
    "10.13.37.19": "Mount Bentang",
    "10.13.37.20": "TelSec This is my final",
}


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
    adapter     TEXT    NOT NULL DEFAULT '{"mode":"positional"}',
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
    flag        TEXT    NOT NULL DEFAULT '',
    collection_status TEXT NOT NULL DEFAULT '',
    submission_status TEXT NOT NULL DEFAULT '',
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
    ('collection_deadline','30'),
    ('concurrency',       '20'),
    ('current_tick',      '0'),
    ('farm_status',       'STOPPED'),
    ('continuous',        '1'),
    ('submit_enabled',    '0'),
    ('submit_mode',       'http_json'),
    ('submit_url',        ''),
    ('submit_token',      ''),
    ('submit_host',       ''),
    ('submit_port',       '31337'),
    ('flag_regex',        'GEMASTIK\\{[^{}\\r\\n]{48}\\}'),
    ('submit_interval',   '5'),
    ('flag_lifetime',     '300'),
    ('team_id',           '17'),
    ('team_name',         'ION lalala'),
    ('team_ip',           '10.13.37.15'),
    ('team_password',     '');

"""


async def init_db() -> None:
    """Create the database schema if it doesn't exist and run safe migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()

        # Migrate existing results table if new columns missing
        async with db.execute("PRAGMA table_info(exploits)") as cur:
            exploit_columns = [r["name"] for r in await cur.fetchall()]
        if "adapter" not in exploit_columns:
            await db.execute("ALTER TABLE exploits ADD COLUMN adapter TEXT NOT NULL DEFAULT '{\"mode\":\"positional\"}'")

        async with db.execute("PRAGMA table_info(results)") as cur:
            columns = [r["name"] for r in await cur.fetchall()]

        if "flag" not in columns:
            await db.execute("ALTER TABLE results ADD COLUMN flag TEXT NOT NULL DEFAULT ''")
        if "collection_status" not in columns:
            await db.execute("ALTER TABLE results ADD COLUMN collection_status TEXT NOT NULL DEFAULT ''")
        if "submission_status" not in columns:
            await db.execute("ALTER TABLE results ADD COLUMN submission_status TEXT NOT NULL DEFAULT ''")

        # Ensure team config defaults
        team_defaults = [
            ("team_id", "17"),
            ("team_name", "ION lalala"),
            ("team_ip", "10.13.37.15"),
            ("team_password", ""),
            ("collection_deadline", "30"),
        ]
        for k, v in team_defaults:
            await db.execute("INSERT OR IGNORE INTO farm_config (key, value) VALUES (?, ?)", (k, v))

        # Replace the application's untouched sample identity with the supplied team.
        sample_values = {"team_id": "1", "team_name": "MyTeam", "team_ip": ""}
        for key, sample in sample_values.items():
            async with db.execute("SELECT value FROM farm_config WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
            if row and row["value"] == sample:
                value = {"team_id": "17", "team_name": "ION lalala", "team_ip": "10.13.37.15"}[key]
                await db.execute("UPDATE farm_config SET value = ? WHERE key = ?", (value, key))

        async with db.execute("SELECT value FROM farm_config WHERE key = 'flag_regex'") as cur:
            regex_row = await cur.fetchone()
        previous_default_regex = r"[A-Za-z0-9_]{2,16}\{[A-Za-z0-9_+=\-/\.]{8,}\}"
        if regex_row and regex_row["value"] == previous_default_regex:
            await db.execute(
                "UPDATE farm_config SET value = ? WHERE key = 'flag_regex'",
                (r"GEMASTIK\{[^{}\r\n]{48}\}",),
            )

        # Normalize saved targets for this competition while retaining service ports.
        for host, name in OPPONENTS.items():
            await db.execute("UPDATE targets SET name = ?, host = ? WHERE host = ? OR name = ?", (name, host, host, name))
        await db.execute("UPDATE targets SET name = 'ION lalala', host = '10.13.37.15', enabled = 0 WHERE host = '10.13.37.15' OR name = 'ION lalala'")
        await db.execute("DELETE FROM targets WHERE host = '10.13.37.21' OR name = 'NPC Team'")

        await db.commit()
