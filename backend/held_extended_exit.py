"""
Pre-market / after-hours exits for positions the bot is HOLDING (not new buys).

Stops and trailing stops only trade in the regular session, so a position held
overnight has no price-based protection until 9:30. Backtests show stocks that
gap up pre-market tend to fade after the open, and gap-downs hit the stops at
the open price. This watches held long positions from 4:00-9:30 and 16:00-20:00 ET:

  * take profit  : pre-market price >= prior close +HELD_EXT_TP_GAP_PCT (and above entry),
                   or position already up >= HELD_EXT_TP_PNL_PCT vs entry -> extended-hours limit sell
  * drop exit    : price <= prior close -HELD_EXT_DROP_PCT (pre-market) -> extended-hours limit sell
  * after hours  : same two rules measured from today's regular close
                   (HELD_EXT_AH_TP_PCT / HELD_EXT_AH_DROP_PCT)
  * at the open  : a take-profit still held HELD_EXT_OPEN_DELAY_SECONDS after 9:30 is sold at market;
                   a drop exit that has recovered gets its trailing stop back instead
  * 19:45 ET     : an unfilled after-hours exit is cancelled and the trailing stop restored

Whole position only. Skips: shorts, symbols news_watch is already exiting, pre-market
news buys made today (pm_quick_exit owns those), after-hours buys made today.
A trigger must hold HELD_EXT_CONFIRM_POLLS polls and pass a fresh-quote check
(last trade <= HELD_EXT_MAX_TRADE_AGE_S old, spread <= HELD_EXT_MAX_SPREAD_PCT).

Switches (Railway variables, read at start-up):
  HELD_EXT_EXIT_ENABLED=true   turn it on (default OFF)
  HELD_EXT_DRY_RUN=true        log what it would do, place nothing
  HELD_EXT_PM_ENABLED=true  HELD_EXT_AH_ENABLED=true
  HELD_EXT_TP_GAP_PCT=2.0  HELD_EXT_TP_PNL_PCT=3.0 (0 = off)  HELD_EXT_DROP_PCT=3.0
  HELD_EXT_AH_TP_PCT=2.0   HELD_EXT_AH_DROP_PCT=4.0
  HELD_EXT_MIN_PROFIT_PCT=0.0  HELD_EXT_CONFIRM_POLLS=2  HELD_EXT_POLL_SECONDS=30
  HELD_EXT_OPEN_DELAY_SECONDS=45  HELD_EXT_MAX_SPREAD_PCT=3.0  HELD_EXT_MAX_TRADE_AGE_S=300
Ledger reason when it closes an entry: held_ext_exit_tp / _drop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend import config
from backend.execution import _get_open_positions
from backend import news_watch as _nw
from backend.news_watch import _extended_exit, _regular_exit, _paced_get, _restore_protection
from backend.services.trade_ledger_service import close_entry, load_ledger, save_ledger, _from_iso

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_STATE: dict[str, dict] = {}
_CLOSE_CACHE: dict[tuple, float] = {}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _on(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(_ET)


def _pt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _open_entry(ledger: dict, sym: str):
    es = ledger.get(sym)
    for e in reversed(es if isinstance(es, list) else []):
        if isinstance(e, dict) and e.get("status") == "open":
            return e
    return None


def _owned_elsewhere(e, today, sec: int) -> bool:
    """True if a pm_quick_exit / after-hours buy made today owns this entry."""
    if not e:
        return False
    md = e.get("metadata") or {}
    t = _from_iso(e.get("created_at"))
    if t is None:
        return False
    t = t.astimezone(_ET)
    if t.date() != today:
        return False
    mins = t.hour * 60 + t.minute
    if md.get("extended_hours") and (str(e.get("strategy")) == "newsbuy" or md.get("source") == "newsbuy"):
        return mins < 9 * 60 + 30 or mins >= 16 * 60  # pre-market buy (pm_quick_exit) / after-hours buy
    return False


async def _snapshot(sym: str):
    base = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
    try:
        r = await _paced_get(f"{base}/v2/stocks/{sym}/snapshot", {"feed": "iex"})
        if r.status_code != 200:
            return None
        s = r.json()
    except Exception:
        logger.exception("held_ext: snapshot failed for %s", sym)
        return None
    if not isinstance(s, dict):
        return None
    tr = s.get("latestTrade") or {}
    price = float(tr.get("p") or 0)
    tdt = _pt(tr.get("t"))
    if price <= 0 or tdt is None:
        return None
    q = s.get("latestQuote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    spread = ((ask - bid) / ((ask + bid) / 2) * 100.0) if bid > 0 and ask > 0 and ask >= bid else None
    daily = s.get("dailyBar") or {}
    ddt = _pt(daily.get("t")) if daily else None
    close = float(daily.get("c") or 0) if (ddt and ddt.astimezone(_ET).date() == _now().date()) else 0.0
    return {"price": price, "age": (datetime.now(timezone.utc) - tdt).total_seconds(), "spread": spread, "close": close}


def _mark(sym: str, kind: str) -> None:
    led = load_ledger()
    e = _open_entry(led, sym)
    if e is not None:
        e.setdefault("metadata", {})["held_ext_exit"] = kind
        save_ledger(led)


def _unmark(sym: str) -> None:
    led = load_ledger()
    e = _open_entry(led, sym)
    if e is not None and (e.get("metadata") or {}).get("held_ext_exit"):
        e["metadata"].pop("held_ext_exit", None)
        save_ledger(led)


def _reconcile(held: set[str]) -> None:
    led = load_ledger()
    changed = False
    for sym, es in led.items():
        for e in es if isinstance(es, list) else []:
            if isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("held_ext_exit") and sym not in held:
                kind = str(e["metadata"]["held_ext_exit"])
                close_entry(led, symbol=sym, order_id=None, exit_price=None, reason="held_ext_exit_" + kind)
                logger.warning("held_ext: %s is out (%s) - ledger entry closed", sym, kind)
                _STATE.pop(sym, None)
                changed = True
                break
    if changed:
        save_ledger(led)


async def _pass() -> None:
    now = _now()
    if now.weekday() >= 5:
        return
    sec = now.hour * 3600 + now.minute * 60 + now.second
    open_s, close_s = 9 * 3600 + 1800, 16 * 3600
    pm = 4 * 3600 <= sec < open_s and _on("HELD_EXT_PM_ENABLED", True)
    ah = close_s <= sec < 20 * 3600 and _on("HELD_EXT_AH_ENABLED", True)
    delay = _f("HELD_EXT_OPEN_DELAY_SECONDS", 45)
    open_win = open_s + delay <= sec < open_s + 1800
    led = load_ledger()
    pending = any(
        isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("held_ext_exit")
        for es in led.values() if isinstance(es, list) for e in es
    )
    if not (pm or ah or (open_win and pending) or (pending and sec >= 19 * 3600 + 2700)):
        if sec < 4 * 3600 and _STATE:
            _STATE.clear()
        return
    positions = await _get_open_positions()
    pos = {}
    for p in positions:
        try:
            if float(p.get("qty") or 0) > 0:
                pos[str(p.get("symbol"))] = p
        except Exception:
            continue
    _reconcile(set(pos))
    dry = _on("HELD_EXT_DRY_RUN", False)
    need = int(_f("HELD_EXT_CONFIRM_POLLS", 2))
    news_busy = set(getattr(_nw, "_actions", {}) or {})
    for sym, p in pos.items():
        e = _open_entry(led, sym)
        st = _STATE.setdefault(sym, {"attempt": 0, "last": 0.0, "hits": 0, "pending": None, "kind": None, "date": now.date()})
        requested = bool(e and (e.get("metadata") or {}).get("held_ext_exit")) or st["kind"]
        try:
            qty = float(p.get("qty") or 0)
            cur = float(p.get("current_price") or 0)
            entry = float(p.get("avg_entry_price") or 0)
            prior = float(p.get("lastday_price") or 0)
        except Exception:
            continue
        if qty <= 0 or cur <= 0 or entry <= 0:
            continue
        # ---- follow-up on an exit already requested ----
        if requested:
            kind = st["kind"] or str((e.get("metadata") or {}).get("held_ext_exit"))
            if open_win:
                if kind == "tp":
                    logger.warning("HELD EXT %s take-profit still held at the open - market sell%s", sym, " [DRY RUN]" if dry else "")
                    if not dry and time.time() - st["last"] >= 50:
                        st["last"] = time.time()
                        try:
                            await _regular_exit(sym, qty)
                        except Exception:
                            logger.exception("held_ext: open exit failed for %s", sym)
                elif time.time() - st["last"] >= 50 and not st.get("restored"):
                    st["last"] = time.time()
                    logger.warning("HELD EXT %s drop exit unfilled at the open - restoring trailing stop%s", sym, " [DRY RUN]" if dry else "")
                    if not dry:
                        await _restore_protection(sym, qty)
                        st["restored"], st["kind"] = True, None
                        _unmark(sym)
            elif ah and sec >= 19 * 3600 + 2700 and not st.get("restored"):
                logger.warning("HELD EXT %s after-hours exit unfilled at 19:45 - restoring trailing stop%s", sym, " [DRY RUN]" if dry else "")
                if not dry:
                    await _restore_protection(sym, qty)
                    st["restored"], st["kind"] = True, None
                    _unmark(sym)
            continue
        # ---- new triggers ----
        if not (pm or ah) or sym in news_busy or _owned_elsewhere(e, now.date(), sec):
            continue
        if pm:
            ref = prior
            tp_gap, drop = _f("HELD_EXT_TP_GAP_PCT", 2.0), _f("HELD_EXT_DROP_PCT", 3.0)
        else:
            ref = _CLOSE_CACHE.get((sym, now.date()), 0.0)
            if ref <= 0:  # today's regular close, fetched once per symbol per day
                snap0 = await _snapshot(sym)
                if snap0 and snap0["close"] > 0:
                    ref = _CLOSE_CACHE[(sym, now.date())] = snap0["close"]
            tp_gap, drop = _f("HELD_EXT_AH_TP_PCT", 2.0), _f("HELD_EXT_AH_DROP_PCT", 4.0)
        pnl = (cur / entry - 1.0) * 100.0
        hit = None
        if ref > 0:
            gap = (cur / ref - 1.0) * 100.0
            if gap >= tp_gap and pnl >= _f("HELD_EXT_MIN_PROFIT_PCT", 0.0):
                hit = "tp"
            elif gap <= -drop:
                hit = "drop"
        tp_pnl = _f("HELD_EXT_TP_PNL_PCT", 3.0)
        if hit is None and pm and tp_pnl > 0 and pnl >= tp_pnl:
            hit = "tp"
        st["hits"] = st["hits"] + 1 if hit and hit == st["pending"] else (1 if hit else 0)
        st["pending"] = hit
        if not hit or st["hits"] < need or time.time() - st["last"] < 50 or st["attempt"] >= 6:
            continue
        snap = await _snapshot(sym)
        if snap is None or snap["age"] > _f("HELD_EXT_MAX_TRADE_AGE_S", 300) or (
                snap["spread"] is not None and snap["spread"] > _f("HELD_EXT_MAX_SPREAD_PCT", 3.0)):
            logger.info("held_ext: %s %s trigger skipped - stale or wide quote (%s)", sym, hit, snap)
            continue
        px = snap["price"]
        logger.warning("HELD EXT %s kind=%s qty=%s px=%.2f entry=%.2f (%+.2f%%) vs ref %.2f (%+.2f%%) attempt=%d%s",
                       sym, hit, qty, px, entry, (px / entry - 1) * 100, ref, (px / ref - 1) * 100 if ref else 0.0,
                       st["attempt"] + 1, " [DRY RUN]" if dry else "")
        st["last"], st["attempt"] = time.time(), st["attempt"] + 1
        if dry:
            continue
        try:
            oid = await _extended_exit(sym, qty, px, st["attempt"] - 1)
        except Exception:
            logger.exception("held_ext: exit failed for %s", sym)
            oid = None
        if oid:
            st["kind"] = hit
            _mark(sym, hit)
            logger.warning("HELD EXT %s order placed id=%s", sym, oid)
        else:
            logger.error("HELD EXT %s order NOT placed - will retry", sym)


async def monitor_held_extended_exit() -> None:
    if not _on("HELD_EXT_EXIT_ENABLED", False):
        logger.info("held_ext off (set HELD_EXT_EXIT_ENABLED=true to turn it on)")
        return
    poll = max(15.0, _f("HELD_EXT_POLL_SECONDS", 30))
    logger.warning("held_ext started - pre-market tp gap +%.1f%% / pnl +%.1f%% / drop -%.1f%%; after-hours tp +%.1f%% / drop -%.1f%%%s",
                   _f("HELD_EXT_TP_GAP_PCT", 2.0), _f("HELD_EXT_TP_PNL_PCT", 3.0), _f("HELD_EXT_DROP_PCT", 3.0),
                   _f("HELD_EXT_AH_TP_PCT", 2.0), _f("HELD_EXT_AH_DROP_PCT", 4.0),
                   " [DRY RUN]" if _on("HELD_EXT_DRY_RUN", False) else "")
    while True:
        try:
            await _pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("held_ext pass failed")
        await asyncio.sleep(poll)
