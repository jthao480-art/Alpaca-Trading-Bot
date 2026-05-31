"""
db/init_db.py – Database initialisation (SQLite via aiosqlite).
"""
from __future__ import annotations
import logging
import os
import aiosqlite

from .migrations import run_migrations

logger = logging.getLogger(__name__)

_DB_PATH: str = ""


async def init_db(db_url: str) -> None:
    global _DB_PATH
    # Only SQLite supported out of the box; swap for asyncpg for Postgres.
    if db_url.startswith("sqlite:///"):
        _DB_PATH = db_url.replace("sqlite:///", "")
    else:
        _DB_PATH = db_url

    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await run_migrations(db)
    logger.info("Database ready at %s", _DB_PATH)


def get_db_path() -> str:
    return _DB_PATH
