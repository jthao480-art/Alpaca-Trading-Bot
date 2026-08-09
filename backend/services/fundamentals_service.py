from __future__ import annotations

from typing import Any

from .auth import alpaca_request_async


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


async def get_snapshot(symbol: str) -> dict[str, Any]:
    try:
        response = await alpaca_request_async(
            "GET",
            f"/v2/stocks/{symbol}/snapshot",
            use_data_api=True,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def score_fundamentals(snapshot: dict[str, Any]) -> float:
    if not isinstance(snapshot, dict) or not snapshot:
        return 0.5

    score = 0.5

    daily = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
    prev_daily = snapshot.get("prevDailyBar") or snapshot.get("previousDailyBar") or snapshot.get("prev_daily_bar") or {}

    close = _safe_float(daily.get("c"))
    open_ = _safe_float(daily.get("o"))
    prev_close = _safe_float(prev_daily.get("c"))

    if close is not None and open_ is not None and open_ > 0:
        intraday_change = (close - open_) / open_
        score += max(min(intraday_change * 2.0, 0.20), -0.20)

    if close is not None and prev_close is not None and prev_close > 0:
        gap_change = (close - prev_close) / prev_close
        score += max(min(gap_change * 1.5, 0.15), -0.15)

    latest_trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
    latest_quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}

    trade_price = _safe_float(latest_trade.get("p"))
    bid = _safe_float(latest_quote.get("bp"))
    ask = _safe_float(latest_quote.get("ap"))

    if trade_price is not None and bid is not None and ask is not None and ask > bid:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid if mid > 0 else 0.0
        if trade_price > mid:
            score += 0.05
        else:
            score -= 0.05
        if spread > 0.01:
            score -= 0.05

    minute_bar = snapshot.get("minuteBar") or snapshot.get("minute_bar") or {}
    minute_vwap = _safe_float(minute_bar.get("vw"))
    minute_close = _safe_float(minute_bar.get("c"))
    if minute_vwap is not None and minute_close is not None and minute_vwap > 0:
        if minute_close > minute_vwap:
            score += 0.05
        else:
            score -= 0.05

    return max(0.0, min(1.0, round(score, 4)))