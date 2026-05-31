"""
db/migrations.py – Schema migrations applied at startup.
"""
from __future__ import annotations
import logging
import aiosqlite

logger = logging.getLogger(__name__)

_MIGRATIONS = [
    # v1 – core tables
    """
    CREATE TABLE IF NOT EXISTS trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT    NOT NULL,
        action        TEXT    NOT NULL,
        qty           REAL    NOT NULL,
        entry_price   REAL    NOT NULL,
        exit_price    REAL,
        take_profit   REAL    NOT NULL,
        stop_loss     REAL    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'open',
        pnl           REAL,
        alpaca_order_id TEXT,
        entry_ts      TEXT    NOT NULL,
        exit_ts       TEXT,
        features      TEXT    DEFAULT '{}',
        metadata      TEXT    DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        model_version TEXT    NOT NULL,
        trained_at    TEXT    NOT NULL,
        n_samples     INTEGER,
        accuracy      REAL,
        notes         TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );
    """,
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    await db.commit()

    row = await (await db.execute("SELECT MAX(version) FROM schema_version")).fetchone()
    current = row[0] if row and row[0] is not None else 0
    logger.info("DB schema version: %d", current)

    for i, sql in enumerate(_MIGRATIONS, start=1):
        if i > current:
            logger.info("Applying migration %d …", i)
            await db.executescript(sql)
            await db.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (i,)
            )
            await db.commit()
            logger.info("Migration %d applied.", i)
