"""
state_store.py – Persistent runtime state (JSON-backed).
Stores open positions, daily PnL, daily-loss flag, and model version.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime, date
from typing import Any, Dict, Optional

from .schemas import StateSnapshot, TradeRecord

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._state: StateSnapshot = StateSnapshot()
        self._today: date = date.today()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def load(self) -> None:
        if not os.path.exists(self._path):
            logger.info("No existing state file – starting fresh.")
            return
        async with _LOCK:
            try:
                with open(self._path, "r") as f:
                    raw = json.load(f)
                self._state = StateSnapshot.model_validate(raw)
                logger.info("State loaded from %s", self._path)
            except Exception:
                logger.exception("Failed to load state; starting fresh.")
                self._state = StateSnapshot()

    async def save(self) -> None:
        async with _LOCK:
            try:
                os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._state.model_dump(mode="json"), f, indent=2, default=str)
                os.replace(tmp, self._path)
            except Exception:
                logger.exception("Failed to save state to %s", self._path)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------
    def _check_day_rollover(self) -> None:
        today = date.today()
        if today != self._today:
            logger.info("Day rollover detected – resetting daily PnL.")
            self._state.daily_pnl = 0.0
            self._state.daily_loss_hit = False
            self._today = today

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def get_position(self, symbol: str) -> Optional[TradeRecord]:
        return self._state.open_positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._state.open_positions

    def add_position(self, trade: TradeRecord) -> None:
        self._state.open_positions[trade.symbol] = trade

    def remove_position(self, symbol: str) -> Optional[TradeRecord]:
        return self._state.open_positions.pop(symbol, None)

    def all_positions(self) -> Dict[str, TradeRecord]:
        return dict(self._state.open_positions)

    # ------------------------------------------------------------------
    # PnL / loss limit
    # ------------------------------------------------------------------
    def add_pnl(self, pnl: float) -> None:
        self._check_day_rollover()
        self._state.daily_pnl += pnl
        if self._state.daily_pnl < 0 and abs(self._state.daily_pnl) >= self._daily_limit:
            self._state.daily_loss_hit = True
            logger.warning("Daily loss limit reached: %.2f", self._state.daily_pnl)

    @property
    def daily_loss_hit(self) -> bool:
        self._check_day_rollover()
        return self._state.daily_loss_hit

    def set_daily_limit(self, limit: float) -> None:
        self._daily_limit = limit

    # ------------------------------------------------------------------
    # Model version
    # ------------------------------------------------------------------
    def set_model_version(self, version: str) -> None:
        self._state.model_version = version

    def get_model_version(self) -> str:
        return self._state.model_version

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> StateSnapshot:
        snap = self._state.model_copy(deep=True)
        snap.timestamp = datetime.utcnow()
        return snap

    def set_last_scan(self) -> None:
        self._state.last_scan_ts = datetime.utcnow()

