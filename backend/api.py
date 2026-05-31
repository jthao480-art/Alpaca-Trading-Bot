"""
api.py – FastAPI REST endpoints for dashboards and history queries.
Complements the WebSocket stream with pull-based access.
"""
from __future__ import annotations
import logging
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db.trades_repo import get_closed_trades, get_open_trades
from backend.schemas import StateSnapshot, TradeRecord

logger = logging.getLogger(__name__)

app = FastAPI(title="TradingBot API", version="3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The state store singleton is injected at startup (set by botv3.py)
_state_store = None


def set_state_store(store) -> None:
    global _state_store
    _state_store = store


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/state", response_model=StateSnapshot)
async def get_state():
    if _state_store is None:
        return StateSnapshot()
    return _state_store.snapshot()


@app.get("/trades/open", response_model=List[TradeRecord])
async def open_trades():
    return await get_open_trades()


@app.get("/trades/closed", response_model=List[TradeRecord])
async def closed_trades(limit: int = 100):
    return await get_closed_trades(limit=limit)
