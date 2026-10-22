"""
Quick exit for pre-market news buys: get in on the news, get out before the fade.

news_buy.py already buys pre-market (marketable limit, extended hours) and then
holds through the open with a trailing stop that only becomes active at 9:30.
Backtests show stocks that are up in pre-market tend to fade after the open, so
this watches those pre-market buys and exits them early:

  * pre-market take-profit : price >= entry +PM_QUICK_TP_PCT  -> extended-hours limit sell
  * pre-market stop        : price <= entry -PM_QUICK_STOP_PCT -> extended-hours limit sell
    (regular stops cannot trigger before 9:30, so this is the only pre-market protection)
  * sell at the open       : whatever is still held PM_OPEN_EXIT_DELAY_SECONDS after 9:30
    is sold at market (retried until 10:00)

Only positions whose ledger entry is a "newsbuy" made in extended hours before 9:30
today are touched. Nothing else in the bot is changed.

Switches (Railway variables, read at start-up):
  PM_QUICK_EXIT_ENABLED=true   turn it on (default OFF)
  PM_QUICK_DRY_RUN=true        log what it would do, place nothing
  PM_QUICK_TP_PCT=2.0  PM_QUICK_STOP_PCT=2.0  PM_QUICK_CONFIRM_POLLS=2
  PM_OPEN_EXIT_ENABLED=true  PM_OPEN_EXIT_DELAY_SECONDS=45  PM_QUICK_POLL_SECONDS=20
Ledger reason when it closes an entry: pm_quick_exit_tp / _stop / _open.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.execution import _get_open_positions
from backend.news_watch import _extended_exit, _regular_exit
from backend.services.trade_ledger_service import close_entry, load_ledger, save_ledger, _from_iso

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_STATE: dict[str, dict] = {}


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


def _candidates(ledger: dict, today) -> dict[str, dict]:
    """symbol -> open ledger entry for a pre-market news buy made today."""
    out: dict[str, dict] = {}
    for sym, entries in ledger.items():
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or e.get("status") != "open":
                continue
            md = e.get("metadata") or {}
            if str(e.get("strategy")) != "newsbuy" and md.get("source") != "newsbuy":
                continue
            if not md.get("extended_hours"):
                continue
            t = _from_iso(e.get("created_at"))
            if t is None:
                continue
            t = t.astimezone(_ET)
            if t.date() != today or t.hour * 60 + t.minute >= 9 * 60 + 30:
                continue
            out[str(sym)] = e
    return out


def _mark_requested(sym: str, kind: str) -> None:
    led = load_ledger()  # fresh read, written straight back (no awaits in between)
    for e in reversed(led.get(sym, []) if isinstance(led.get(sym), list) else []):
        if isinstance(e, dict) and e.get("status") == "open":
            e.setdefault("metadata", {})["pm_exit"] = kind
            break
    save_ledger(led)


def _reconcile(held: set[str]) -> None:
    led = load_ledger()
    changed = False
    for sym, entries in led.items():
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("pm_exit") and sym not in held:
                kind = str(e["metadata"]["pm_exit"])
                close_entry(led, symbol=sym, order_id=None, exit_price=None, reason="pm_quick_exit_" + kind)
                logger.warning("pm_quick_exit: %s is out (%s) — ledger entry closed", sym, kind)
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
    delay = _f("PM_OPEN_EXIT_DELAY_SECONDS", 45)
    pre_start, open_s, open_end = 4 * 3600, 9 * 3600 + 1800, 10 * 3600
    led0 = load_ledger()
    pending = any(
        isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("pm_exit")
        for es in led0.values() if isinstance(es, list) for e in es
    )
    if not (pre_start <= sec < open_end + 600) and not pending:
        return
    cands = _candidates(led0, now.date())
    positions = await _get_open_positions()
    pos = {}
    for p in positions:
        try:
            if float(p.get("qty") or 0) > 0:
                pos[str(p.get("symbol"))] = p
        except Exception:
            continue
    _reconcile(set(pos))
    if not cands:
        return
    dry = _on("PM_QUICK_DRY_RUN", False)
    tp, stop = _f("PM_QUICK_TP_PCT", 2.0) / 100.0, _f("PM_QUICK_STOP_PCT", 2.0) / 100.0
    need = int(_f("PM_QUICK_CONFIRM_POLLS", 2))
    for sym, e in cands.items():
        p = pos.get(sym)
        if p is None:
            continue
        st = _STATE.setdefault(sym, {"attempt": 0, "last": 0.0, "hits": 0, "kind": None})
        try:
            qty = float(p.get("qty") or 0)
            cur = float(p.get("current_price") or 0)
            ep = float(e.get("entry_price") or p.get("avg_entry_price") or 0)
        except Exception:
            continue
        if qty <= 0 or cur <= 0 or ep <= 0:
            continue
        chg = cur / ep - 1.0
        kind = None
        if sec < open_s:
            hit = "tp" if chg >= tp else ("stop" if chg <= -stop else None)
            st["hits"] = st["hits"] + 1 if hit and hit == st.get("pending") else (1 if hit else 0)
            st["pending"] = hit
            if hit and st["hits"] >= need:
                kind = hit
        elif _on("PM_OPEN_EXIT_ENABLED", True) and open_s + delay <= sec < open_end:
            kind = "open"
        if kind is None:
            continue
        if time.time() - st["last"] < 50 or st["attempt"] >= 6:
            continue
        logger.warning("PM QUICK EXIT %s kind=%s qty=%s px=%.2f entry=%.2f (%+.2f%%) attempt=%d%s",
                       sym, kind, qty, cur, ep, chg * 100, st["attempt"] + 1, " [DRY RUN]" if dry else "")
        st["last"], st["attempt"], st["kind"] = time.time(), st["attempt"] + 1, kind
        if dry:
            continue
        try:
            if sec < open_s:
                oid = await _extended_exit(sym, qty, cur, st["attempt"] - 1)
            else:
                oid = await _regular_exit(sym, qty)
        except Exception:
            logger.exception("pm_quick_exit: exit failed for %s", sym)
            oid = None
        if oid:
            _mark_requested(sym, kind)
            logger.warning("PM QUICK EXIT %s order placed id=%s", sym, oid)
        else:
            logger.error("PM QUICK EXIT %s order NOT placed — will retry", sym)


async def monitor_pm_quick_exit() -> None:
    if not _on("PM_QUICK_EXIT_ENABLED", False):
        logger.info("pm_quick_exit off (set PM_QUICK_EXIT_ENABLED=true to turn it on)")
        return
    poll = max(10.0, _f("PM_QUICK_POLL_SECONDS", 20))
    logger.warning("pm_quick_exit started — pre-market take-profit +%.1f%% / stop -%.1f%%, sell at the open %s (+%.0fs)%s",
                   _f("PM_QUICK_TP_PCT", 2.0), _f("PM_QUICK_STOP_PCT", 2.0),
                   "ON" if _on("PM_OPEN_EXIT_ENABLED", True) else "OFF", _f("PM_OPEN_EXIT_DELAY_SECONDS", 45),
                   " [DRY RUN]" if _on("PM_QUICK_DRY_RUN", False) else "")
    while True:
        try:
            await _pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pm_quick_exit pass failed")
        await asyncio.sleep(poll)
