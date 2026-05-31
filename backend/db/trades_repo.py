"""
db/trades_repo.py – Async repository for trades and learning runs.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import List, Optional

import aiosqlite

from .init_db import get_db_path
from backend.schemas import LearningSummary, TradeRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _row_to_trade(row) -> TradeRecord:
    (id_, symbol, action, qty, entry_price, exit_price, take_profit,
     stop_loss, status, pnl, alpaca_order_id, entry_ts, exit_ts,
     features_str, metadata_str) = row
    return TradeRecord(
        id=id_,
        symbol=symbol,
        action=action,
        qty=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        status=status,
        pnl=pnl,
        alpaca_order_id=alpaca_order_id,
        entry_ts=datetime.fromisoformat(entry_ts),
        exit_ts=datetime.fromisoformat(exit_ts) if exit_ts else None,
        features=json.loads(features_str or "{}"),
        metadata=json.loads(metadata_str or "{}"),
    )


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------
async def insert_trade(trade: TradeRecord) -> int:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            """
            INSERT INTO trades
              (symbol, action, qty, entry_price, exit_price, take_profit,
               stop_loss, status, pnl, alpaca_order_id, entry_ts, exit_ts,
               features, metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.symbol, trade.action, trade.qty, trade.entry_price,
                trade.exit_price, trade.take_profit, trade.stop_loss,
                trade.status, trade.pnl, trade.alpaca_order_id,
                trade.entry_ts.isoformat(),
                trade.exit_ts.isoformat() if trade.exit_ts else None,
                json.dumps(trade.features), json.dumps(trade.metadata),
            ),
        )
        await db.commit()
        row_id = cursor.lastrowid
        return int(row_id) if row_id is not None else 0

async def update_trade_exit(
    trade_id: int,
    exit_price: float,
    pnl: float,
    exit_ts: Optional[datetime] = None,
) -> None:
    exit_ts = exit_ts or datetime.utcnow()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            UPDATE trades
               SET exit_price=?, pnl=?, status='closed', exit_ts=?
             WHERE id=?
            """,
            (exit_price, pnl, exit_ts.isoformat(), trade_id),
        )
        await db.commit()


async def get_closed_trades(limit: int = 1000) -> List[TradeRecord]:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_open_trades() -> List[TradeRecord]:
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_ts DESC"
        )
        rows = await cursor.fetchall()
    return [_row_to_trade(r) for r in rows]


# ---------------------------------------------------------------------------
# Learning runs
# ---------------------------------------------------------------------------
async def insert_learning_run(summary: LearningSummary) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO learning_runs (model_version, trained_at, n_samples, accuracy, notes)
            VALUES (?,?,?,?,?)
            """,
            (
                summary.model_version,
                summary.trained_at.isoformat(),
                summary.n_samples,
                summary.accuracy,
                summary.notes,
            ),
        )
        await db.commit()
