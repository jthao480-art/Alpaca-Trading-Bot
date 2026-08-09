from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        return None


def _format_date(value: date | datetime | str | None) -> str | None:
    d = _to_date(value)
    return d.isoformat() if d else None


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


class PriceService:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or POLYGON_API_KEY

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY is not set")

        params = dict(params or {})
        params["apiKey"] = self.api_key

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}

    async def get_daily_close(self, ticker: str, on_date: date | datetime | str) -> float | None:
        d = _format_date(on_date)
        if not d:
            return None

        url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker.upper()}/range/1/day/{d}/{d}"
        try:
            data = await self._get(url, params={"adjusted": "true", "sort": "asc", "limit": 50})
            results = data.get("results") or []
            if not results:
                return None
            bar = results[-1]
            return _safe_float(bar.get("c"))
        except Exception:
            logger.exception("Failed to fetch daily close for %s on %s", ticker, d)
            return None

    async def get_price_on_or_after(
        self,
        ticker: str,
        on_date: date | datetime | str,
        lookahead_days: int = 10,
    ) -> float | None:
        d = _to_date(on_date)
        if d is None:
            return None

        end = d + timedelta(days=max(0, lookahead_days))
        url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker.upper()}/range/1/day/{d.isoformat()}/{end.isoformat()}"
        try:
            data = await self._get(url, params={"adjusted": "true", "sort": "asc", "limit": 50})
            results = data.get("results") or []
            if not results:
                return None
            for bar in results:
                close = _safe_float(bar.get("c"))
                if close is not None:
                    return close
            return None
        except Exception:
            logger.exception("Failed to fetch price on/after %s for %s", d, ticker)
            return None

    async def get_price_after_days(
        self,
        ticker: str,
        on_date: date | datetime | str,
        days: int = 30,
    ) -> float | None:
        d = _to_date(on_date)
        if d is None:
            return None

        target = d + timedelta(days=max(1, int(days)))
        return await self.get_price_on_or_after(ticker=ticker, on_date=target, lookahead_days=10)

    async def get_return_pct(
        self,
        ticker: str,
        buy_date: date | datetime | str,
        sell_date: date | datetime | str,
    ) -> float | None:
        buy = await self.get_price_on_or_after(ticker, buy_date, lookahead_days=10)
        sell = await self.get_price_on_or_after(ticker, sell_date, lookahead_days=10)

        if buy is None or sell is None or buy <= 0:
            return None

        return (sell - buy) / buy

    async def get_forward_returns(
        self,
        ticker: str,
        trade_date: date | datetime | str,
        horizons: tuple[int, ...] = (30, 60, 90),
    ) -> dict[int, float | None]:
        out: dict[int, float | None] = {}
        for horizon in horizons:
            try:
                out[horizon] = await self.get_price_after_days(ticker=ticker, on_date=trade_date, days=horizon)
            except Exception:
                out[horizon] = None
        return out