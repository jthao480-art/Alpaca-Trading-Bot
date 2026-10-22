"""
Pre-market PRICE-signal buyer (no news needed), paired with pm_quick_exit.py.

Looks at the whole tradable universe 4:05-9:15 ET and buys a liquid stock that is
up PM_SIGNAL_MIN_GAIN_PCT..PM_SIGNAL_MAX_GAIN_PCT (default 2..7%) from the prior close
with real pre-market volume and a tight spread, confirmed on consecutive scans.
Orders, protection and ledger handling are the same as news_buy's pre-market buy
(marketable limit, unfilled remainder cancelled, GTC hard stop parked, trailing stop
queued for the open). The ledger entry is a "newsbuy" with tier "P" and
extended_hours=True, so pm_quick_exit.py manages the exit (take-profit / stop in
pre-market, sell at the open) and the news-buy bookkeeping protects the entry.

Backtest note (20 trading days, price-only signals, 0.3% each-way slippage): the
average result was slightly negative after costs, and gaps above ~7% did worse. This
exists to collect live forward data on paper; keep it small.

Switches (Railway variables, read at start-up):
  PM_SIGNAL_ENABLED=true          turn it on (default OFF)
  PM_SIGNAL_DRY_RUN=true          log the buys it would make, place nothing
  PM_SIGNAL_MIN_GAIN_PCT=2  PM_SIGNAL_MAX_GAIN_PCT=7  PM_SIGNAL_MIN_VOL=1000
  PM_SIGNAL_MAX_PER_DAY=3   PM_SIGNAL_USD=500   PM_SIGNAL_CONFIRM_SCANS=2
  PM_SIGNAL_MAX_SPREAD_PCT=1.0  PM_SIGNAL_MIN_PREV_DOLLAR_VOL=500000
  PM_SIGNAL_START_MINUTE_ET=245  PM_SIGNAL_END_MINUTE_ET=555  (minutes after midnight)
  PM_SIGNAL_FULL_SCAN_MINUTES=5  PM_SIGNAL_TRAIL_PCT=2.0  PM_SIGNAL_LIMIT_SLIP_PCT=0.5
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend import config
from backend.execution import _get_account, _get_open_positions
from backend.news_buy import _asset_ok, _buy_lock, _extended_buy, _parse_ts
from backend.news_watch import _paced_get
from backend.services.trade_ledger_service import add_entry, load_ledger, save_ledger

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_seen: dict[str, int] = {}
_watch: set[str] = set()
_dry_logged: set[tuple] = set()
_last_full = 0.0


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _on(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(_ET)


def _evaluate(sym: str, s: dict, today) -> Optional[dict]:
    """Snapshot -> candidate dict if it passes every price/volume/quote filter."""
    trade = s.get("latestTrade") or {}
    price = float(trade.get("p") or 0)
    tdt = _parse_ts(trade.get("t"))
    if price <= 0 or tdt is None:
        return None
    daily, prev = s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
    ddt = _parse_ts(daily.get("t")) if daily else None
    d_today = bool(daily) and ddt is not None and ddt.astimezone(_ET).date() == today
    ref = prev if d_today or not daily else daily
    if not ref or not float(ref.get("c") or 0):
        return None
    ref_close = float(ref["c"])
    chg = (price / ref_close - 1.0) * 100.0
    q = s.get("latestQuote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    spread = ((ask - bid) / ((ask + bid) / 2) * 100.0) if bid > 0 and ask >= bid else None
    return {
        "sym": sym, "price": price, "ref": ref_close, "chg": chg, "spread": spread,
        "vol": float(daily.get("v") or 0) if d_today else 0.0,
        "prev_dv": ref_close * float(ref.get("v") or 0),
        "age": (datetime.now(timezone.utc) - tdt).total_seconds(),
    }


def _passes(c: dict) -> Optional[str]:
    """None if it qualifies, else the reason it does not."""
    lo, hi = _f("PM_SIGNAL_MIN_GAIN_PCT", 2.0), _f("PM_SIGNAL_MAX_GAIN_PCT", 7.0)
    if not (lo <= c["chg"] <= hi):
        return "gain"
    if c["price"] < max(float(getattr(config, "MIN_PRICE", 4.0)), 3.0):
        return "price"
    if c["vol"] < _f("PM_SIGNAL_MIN_VOL", 1000):
        return "volume"
    if c["prev_dv"] < _f("PM_SIGNAL_MIN_PREV_DOLLAR_VOL", 500000):
        return "prev liquidity"
    if c["age"] > 180:
        return "stale trade"
    if c["spread"] is None or c["spread"] > _f("PM_SIGNAL_MAX_SPREAD_PCT", 1.0):
        return "spread"
    return None


async def _snapshots(symbols: list[str]) -> dict[str, dict]:
    base = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 500):
        try:
            resp = await _paced_get(f"{base}/v2/stocks/snapshots", {"symbols": ",".join(symbols[i:i + 500]), "feed": "iex"})
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                data = data.get("snapshots", data) if isinstance(data, dict) else {}
                out.update({k: v for k, v in data.items() if isinstance(v, dict)})
            else:
                logger.info("pm_signal: snapshots HTTP %s", getattr(resp, "status_code", None))
        except Exception:
            logger.exception("pm_signal: snapshot batch failed")
    return out


def _bought_today(ledger: dict, today) -> tuple[int, set[str]]:
    n, syms = 0, set()
    for sym, entries in ledger.items():
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            ca = _parse_ts(e.get("created_at"))
            if ca is None or ca.astimezone(_ET).date() != today:
                continue
            syms.add(str(sym))
            if str(e.get("strategy", "")).lower() == "newsbuy" and (e.get("metadata") or {}).get("tier") == "P":
                n += 1
    return n, syms


async def _buy(c: dict) -> bool:
    sym = c["sym"]
    acct = await _get_account()
    if not acct:
        return False
    limit = round(c["price"] * (1.0 + _f("PM_SIGNAL_LIMIT_SLIP_PCT", 0.5) / 100.0), 2)
    qty = int(_f("PM_SIGNAL_USD", 500) // limit)
    if qty < 1:
        logger.info("PM SIGNAL %s skipped — $%.0f too small for price %.2f", sym, _f("PM_SIGNAL_USD", 500), limit)
        return False
    tag = "%+.1f%% vs prior close, vol %d, spread %.2f%%" % (c["chg"], c["vol"], c["spread"] or 0.0)
    if _on("PM_SIGNAL_DRY_RUN", False):
        if (_now().date(), sym) not in _dry_logged:
            _dry_logged.add((_now().date(), sym))
            logger.warning("PM SIGNAL BUY [DRY RUN] %s qty=%d @~%.2f (%s)", sym, qty, limit, tag)
        return False
    try:
        from backend.botv3 import _BOUGHT_THIS_SESSION, _PENDING_BUYS  # type: ignore
    except Exception:
        _BOUGHT_THIS_SESSION, _PENDING_BUYS = set(), set()
    trail = _f("PM_SIGNAL_TRAIL_PCT", 2.0)
    _PENDING_BUYS.add(sym)
    _BOUGHT_THIS_SESSION.add(sym)
    try:
        async with _buy_lock:
            oid, avg, filled = await _extended_buy(sym, qty, limit, trail)
    finally:
        _PENDING_BUYS.discard(sym)
    if not filled or filled <= 0:
        return False
    fill = avg or limit
    ledger = load_ledger()
    add_entry(
        ledger, symbol=sym, side="long", qty=filled, entry_price=fill, order_id=oid, strategy="newsbuy",
        metadata={
            "source": "newsbuy", "tier": "P", "news_reason": "premarket price signal",
            "headline": "PRICE SIGNAL " + tag, "chg_vs_prior_close_pct": round(c["chg"], 2),
            "extended_hours": True, "use_trailing": True, "trailing_stop_pct": trail, "pm_signal": True,
        },
    )
    save_ledger(ledger)
    logger.warning("PM SIGNAL BUY %s qty=%d @%.2f (%s)", sym, int(filled), fill, tag)
    return True


async def _scan_once() -> None:
    global _last_full
    now = _now()
    if now.weekday() >= 5:
        return
    m = now.hour * 60 + now.minute
    if not (_f("PM_SIGNAL_START_MINUTE_ET", 245) <= m < _f("PM_SIGNAL_END_MINUTE_ET", 555)):
        _seen.clear()
        return
    ledger = load_ledger()
    n_today, held_today = _bought_today(ledger, now.date())
    if n_today >= int(_f("PM_SIGNAL_MAX_PER_DAY", 3)):
        return
    full = time.time() - _last_full >= _f("PM_SIGNAL_FULL_SCAN_MINUTES", 5) * 60 or not _watch
    universe = list(getattr(config, "SYMBOL_UNIVERSE", []) or [])
    symbols = universe if full else sorted(_watch)
    if not symbols:
        return
    snaps = await _snapshots(symbols)
    if full:
        _last_full = time.time()
        _watch.clear()
    cands, up2, fails = [], 0, {}
    for sym, s in snaps.items():
        try:
            c = _evaluate(sym, s, now.date())
        except Exception:
            continue
        if c is None:
            continue
        if c["chg"] >= 1.0:
            _watch.add(sym)
        if c["chg"] >= _f("PM_SIGNAL_MIN_GAIN_PCT", 2.0):
            up2 += 1
            why = _passes(c)
            if why:
                fails[why] = fails.get(why, 0) + 1
            else:
                cands.append(c)
    logger.info("pm_signal scan (%s): %d symbols, %d up >= %.0f%%, %d pass, filtered by %s",
                "full" if full else "watch", len(snaps), up2, _f("PM_SIGNAL_MIN_GAIN_PCT", 2.0), len(cands), fails or "-")
    live = {c["sym"] for c in cands}
    for k in list(_seen):
        if k not in live:
            _seen.pop(k, None)
    need = int(_f("PM_SIGNAL_CONFIRM_SCANS", 2))
    positions = None
    for c in sorted(cands, key=lambda z: -z["vol"]):
        sym = c["sym"]
        _seen[sym] = _seen.get(sym, 0) + 1
        if _seen[sym] < need or sym in held_today:
            continue
        if n_today >= int(_f("PM_SIGNAL_MAX_PER_DAY", 3)):
            break
        if positions is None:
            positions = await _get_open_positions()
        if any(str(p.get("symbol", "")).upper() == sym for p in positions):
            continue
        if sym in str(getattr(config, "ENTRY_BLOCKLIST", "")).split(","):
            continue
        if len(positions) >= int(float(getattr(config, "MAX_POSITIONS", 25))):
            break
        if not await _asset_ok(sym):
            continue
        if await _buy(c):
            n_today += 1
            held_today.add(sym)


async def monitor_pm_signal_buy() -> None:
    if not _on("PM_SIGNAL_ENABLED", False):
        logger.info("pm_signal_buy off (set PM_SIGNAL_ENABLED=true to turn it on)")
        return
    logger.warning("pm_signal_buy started — pre-market buys %.0f..%.0f%% above the prior close, max %d/day, $%.0f each%s",
                   _f("PM_SIGNAL_MIN_GAIN_PCT", 2.0), _f("PM_SIGNAL_MAX_GAIN_PCT", 7.0), int(_f("PM_SIGNAL_MAX_PER_DAY", 3)),
                   _f("PM_SIGNAL_USD", 500), " [DRY RUN]" if _on("PM_SIGNAL_DRY_RUN", False) else "")
    while True:
        try:
            await _scan_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pm_signal_buy scan failed")
        await asyncio.sleep(max(30.0, _f("PM_SIGNAL_POLL_SECONDS", 60)))
