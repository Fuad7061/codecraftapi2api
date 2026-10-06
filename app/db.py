"""
SQLite storage (aiosqlite). Idempotent init — safe on every deploy.
Accounts, settings and logs live in DB_PATH (mount /app/data as a volume).
"""
import logging
import os
import secrets

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)
DB_PATH = settings.DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    cf_clearance TEXT NOT NULL,
    remember_name TEXT NOT NULL,
    remember_value TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    success_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    last_used TEXT,
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT,
    model TEXT,
    stream INTEGER,
    status_code INTEGER,
    duration_ms INTEGER,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# key -> default (INSERT OR IGNORE: never overwrites user values)
DEFAULTS = {
    "api_keys": settings.API_KEY,
    "dashboard_password": settings.DASHBOARD_PASSWORD,
    "models": "gemini-3.1-pro",
    "default_model": "gemini-3.1-pro",
    "strategy": "round_robin",  # round_robin | random | least_used
    "request_timeout": "300",
    "max_failures": "3",
    "impersonate": "chrome120",
    "user_agent": "",
    "proxy": "",
    "upstream_base": "https://codecraftapi.com",
    "reasoning_field": "reasoning",  # or reasoning_content
    "default_temperature": "",  # empty = auto (not sent upstream unless the client sends it)
    "default_max_tokens": "",    # empty = auto
    "enable_logging": "1",
    "keepalive_minutes": "30",
    "log_retention_days": "3",   # 0 = keep forever
    "plan_refresh_minutes": "10",
}

# Added after first release: (column, type)
ACCOUNT_MIGRATIONS = [
    ("plan_name", "TEXT"),
    ("plan_remaining", "INTEGER"),   # -1 = unlimited, NULL = unknown
    ("plan_total", "INTEGER"),
    ("plan_used", "INTEGER"),
    ("balance", "REAL"),
    ("plan_checked_at", "TEXT"),
]


async def init_db() -> None:
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(accounts)") as c:
            have = {r[1] for r in await c.fetchall()}
        for col, typ in ACCOUNT_MIGRATIONS:
            if col not in have:
                await db.execute(f"ALTER TABLE accounts ADD COLUMN {col} {typ}")
        # one-time: old builds forced temperature=1.0 / max_tokens=8192; switch to auto
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('migr_auto_defaults', '0')")
        async with db.execute("SELECT value FROM config WHERE key='migr_auto_defaults'") as c:
            if (await c.fetchone())[0] == "0":
                await db.execute("UPDATE config SET value='' WHERE key='default_temperature' AND value='1.0'")
                await db.execute("UPDATE config SET value='' WHERE key='default_max_tokens' AND value='8192'")
                await db.execute("UPDATE config SET value='1' WHERE key='migr_auto_defaults'")
        for k, v in DEFAULTS.items():
            await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
        await db.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES ('secret_key', ?)",
            (secrets.token_hex(32),),
        )
        # Seed an account from env constants if table is empty
        async with db.execute("SELECT COUNT(*) FROM accounts") as c:
            n = (await c.fetchone())[0]
        if n == 0 and settings.CF_CLEARANCE and settings.REMEMBER_WEB_NAME and settings.REMEMBER_WEB_VALUE:
            await db.execute(
                "INSERT INTO accounts (name, cf_clearance, remember_name, remember_value) VALUES (?,?,?,?)",
                ("env-account", settings.CF_CLEARANCE, settings.REMEMBER_WEB_NAME, settings.REMEMBER_WEB_VALUE),
            )
            logger.info("[DB] Seeded account from environment")
        await db.commit()
    logger.info("[DB] Ready at %s", DB_PATH)


async def fetchall(sql: str, args: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as c:
            return [dict(r) for r in await c.fetchall()]


async def fetchone(sql: str, args: tuple = ()) -> dict | None:
    rows = await fetchall(sql, args)
    return rows[0] if rows else None


async def execute(sql: str, args: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def vacuum() -> None:
    """Reclaim disk space after bulk deletes."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("VACUUM")

async def cleanup_logs(days: int = 3) -> None:
    """Auto-cleanup logs older than `days`."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM request_logs WHERE created_at < datetime('now', ?)", (f"-{days} days",))
        await db.commit()


async def get_config() -> dict:
    rows = await fetchall("SELECT key, value FROM config")
    cfg = {r["key"]: r["value"] for r in rows}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


async def set_config(values: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for k, v in values.items():
            await db.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )
        await db.commit()
