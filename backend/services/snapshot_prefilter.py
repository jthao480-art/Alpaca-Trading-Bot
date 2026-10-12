"""
Alpaca snapshot prefilter for the scan.

Why: every scan cycle used to run the agents (Ares / Wave / Intraday, several bar
requests each) on 2,000 random symbols, which is far more than the data API's
rate limit allows -- a cycle took an hour or more, most agent calls failed with
429, and buys (processed only at the end of a cycle) went quiet.

What: Alpaca's multi-symbol snapshot endpoint returns price, today's open and the
prior close for ~500 symbols per request, so the whole universe costs ~14
requests. Every signal the bot trades needs the stock to be DOWN:
  Ripple / Ares provisional : price < prior close
  Wave provisional          : price <= open * (1 - 1%)
  legacy Ares / Wave agents : the daily down-streak, which requires today < prior close
so a symbol that is neither below its prior close nor >= 1% under its open cannot
produce a signal and is skipped before any bar request is made. Penny stocks
below MIN_PRICE (which the entry step drops anyway) are skipped too.

Safety:
  * held symbols (and Tradetiq-curated ones) are always kept -- exits still need signals;
  * a snapshot chunk that fails is kept whole (fail open), never silently dropped;
  * tolerance (SCAN_PREFILTER_TOLERANCE_PCT, default 0.5%) keeps borderline names so
    a few cents of movement between this snapshot and the agent check can't drop one;
  * bypassed outside regular hours, and by the caller when the default agents
    (News/Momentum/... which also trade up-moves) are enabled.
SCAN_PREFILTER_ENABLED=false turns it off entirely.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from backend import config

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_WAVE_MOMENTUM_THRESHOLD = -0.01   # mirrors intraday_agent.WAVE_MOMENTUM_THRESHOLD


def _f(name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except Exception:
        return default


def _parse_ts(s: Any):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def evaluate(
    snapshots: dict[str, Any],
    today,
    min_price: float,
    tolerance_pct: float,
) -> tuple[set[str], dict[str, int]]:
    """Pure function: which symbols in `snapshots` could produce a signal."""
    keep: set[str] = set()
    stats = {"seen": 0, "no_trade_today": 0, "penny": 0, "not_down": 0, "kept": 0}
    tol = tolerance_pct / 100.0
    for sym, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        stats["seen"] += 1
        try:
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}
            price = float((snap.get("latestTrade") or {}).get("p") or daily.get("c") or 0)
            ddt = _parse_ts(daily.get("t")) if daily else None
            traded_today = bool(daily) and ddt is not None and ddt.astimezone(_ET).date() == today
            if not traded_today:
                stats["no_trade_today"] += 1
                continue
            ref_close = float(prev.get("c") or 0)
            day_open = float(daily.get("o") or 0)
            if price <= 0 or ref_close <= 0:
                stats["no_trade_today"] += 1
                continue
            if price < min_price * 0.9:
                stats["penny"] += 1
                continue
            below_prior = price <= ref_close * (1.0 + tol)
            below_open = day_open > 0 and price <= day_open * (1.0 + _WAVE_MOMENTUM_THRESHOLD + tol)
            if below_prior or below_open:
                keep.add(sym.upper())
                stats["kept"] += 1
            else:
                stats["not_down"] += 1
        except Exception:
            keep.add(sym.upper())          # unparseable -> fail open
            stats["kept"] += 1
    return keep, stats


async def _fetch_chunk(chunk: list[str]) -> dict[str, Any] | None:
    from backend.services.auth import alpaca_request_async
    from backend.services.bars_service import _service as _bars

    params = {"symbols": ",".join(chunk), "feed": "iex"}
    for attempt in range(1, 4):
        await _bars._wait_for_slot()
        resp = await alpaca_request_async("GET", "/v2/stocks/snapshots", params=params, use_data_api=True)
        if resp.status_code == 429:
            _bars._note_rate_limit(resp, 5.0 * attempt, "/v2/stocks/snapshots", attempt)
            continue
        if resp.status_code != 200:
            logger.warning("prefilter: snapshots HTTP %s %s", resp.status_code, resp.text[:150])
            return None
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
            data = data["snapshots"]
        return data if isinstance(data, dict) else None
    return None


async def prefilter_symbols(symbols: list[str], always_keep: Iterable[str] = ()) -> list[str]:
    """Return the subset of `symbols` that could signal today (order preserved)."""
    if not symbols:
        return symbols
    chunk_size = max(50, int(_f("SCAN_PREFILTER_CHUNK", 500)))
    tol = _f("SCAN_PREFILTER_TOLERANCE_PCT", 0.5)
    min_price = _f("MIN_PRICE", 4.0)
    today = datetime.now(_ET).date()
    always = {s.upper() for s in always_keep}
    chunks = [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    keep: set[str] = set()
    total = {"seen": 0, "no_trade_today": 0, "penny": 0, "not_down": 0, "kept": 0}
    failed = 0
    for chunk in chunks:
        try:
            snaps = await _fetch_chunk(chunk)
        except Exception:
            logger.exception("prefilter: snapshot chunk raised")
            snaps = None
        if snaps is None:
            failed += 1
            keep.update(s.upper() for s in chunk)      # fail open
            continue
        k, st = evaluate(snaps, today, min_price, tol)
        keep |= k
        for key in total:
            total[key] += st[key]
    out = [s for s in symbols if s.upper() in keep or s.upper() in always]
    logger.info(
        "Scan prefilter: universe=%d kept=%d | snapshots=%d not_down=%d penny=%d no_trade_today=%d | "
        "chunks=%d failed(open)=%d held/curated=%d",
        len(symbols), len(out), total["seen"], total["not_down"], total["penny"], total["no_trade_today"],
        len(chunks), failed, len(always),
    )
    return out
