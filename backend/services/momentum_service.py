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


async def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 40) -> dict[str, Any]:
    try:
        response = await alpaca_request_async(
            "GET",
            f"/v2/stocks/{symbol}/bars",
            params={
                "timeframe": timeframe,
                "limit": limit,
                "feed": "iex",
                "sort": "asc",
            },
            use_data_api=True,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def score_momentum(bars_payload: dict[str, Any]) -> float:
    if not isinstance(bars_payload, dict) or not bars_payload:
        return 0.5

    bars = bars_payload.get("bars") or bars_payload.get("data") or []
    if not isinstance(bars, list) or len(bars) < 5:
        return 0.5

    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []

    for bar in bars:
        if not isinstance(bar, dict):
            continue
        c = _safe_float(bar.get("c"))
        h = _safe_float(bar.get("h"))
        l = _safe_float(bar.get("l"))
        v = _safe_float(bar.get("v"))
        if c is not None:
            closes.append(c)
        if h is not None:
            highs.append(h)
        if l is not None:
            lows.append(l)
        if v is not None:
            volumes.append(v)

    if len(closes) < 5:
        return 0.5

    score = 0.5
    first = closes[0]
    last = closes[-1]

    if first > 0:
        ret = (last - first) / first
        score += max(min(ret * 3.0, 0.25), -0.25)

    mid = len(closes) // 2
    if mid > 0 and closes[mid] > 0:
        early = closes[mid]
        late = closes[-1]
        seg_ret = (late - early) / early
        score += max(min(seg_ret * 2.0, 0.15), -0.15)

    if highs and lows:
        range_ = max(highs) - min(lows)
        if range_ > 0 and last > 0:
            position = (last - min(lows)) / range_
            score += (position - 0.5) * 0.10

    if len(volumes) >= 2:
        avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        cur_vol = volumes[-1]
        if avg_vol > 0:
            vol_ratio = cur_vol / avg_vol
            if vol_ratio > 1.2:
                score += 0.05
            elif vol_ratio < 0.8:
                score -= 0.05

    return max(0.0, min(1.0, round(score, 4)))