"""
Pre-market take-profit for positions held overnight - resting limit sells, no price feed needed.

The bot only sees IEX prices, which show almost no pre-market trades, so a rule that WATCHES
pre-market prices can't fire. A resting extended-hours limit order doesn't need to watch anything:
the exchange fills it if the stock trades at or above the price.

Backtest (SIP prices, paper ledger, 781 positions held overnight, rule applied on the FIRST night
only, compared with what actually happened): selling in the pre-market at
    gap +2% on the prior close and above entry,  OR  +3% on entry
beat the real result by about +$2,600 (39 fired, 71% better, positive in both halves). On the much
smaller cash ledger (234 positions) the same rule was slightly negative (-$96, 17 fired), so it is OFF
by default on both bots.

What this does (04:05 ET onwards, weekdays):
  1. For each long position held from the previous trading day (not bought today), work out the
     take-profit price = min( max(prior_close*(1+GAP), entry), entry*(1+PNL) ) and put a DAY limit
     sell for the whole position with extended_hours=true. To free the shares the position's stop
     orders are cancelled first; the highest stop price and the trailing % are remembered.
  2. At 09:30 + PM_TP_RESTORE_SECONDS the limit order is cancelled and the protection is put back:
     if the price is already at or under the remembered stop price the position is sold at market
     (that is what the stop would have done at the open); otherwise a trailing stop is placed whose
     distance reproduces the old stop level (never wider than the old trail %).
  3. If the limit filled, the ledger entry is closed with reason premarket_tp.
Restart-safe: what was placed is saved on the ledger entry (metadata.pm_tp); after 09:30:10 any
position still carrying an un-restored pm_tp gets its protection back at once.

Switches (Railway variables, read at start-up):
  PM_TP_ENABLED=true        turn it on (default OFF)
  PM_TP_DRY_RUN=true        log what it would place, change nothing
  PM_TP_GAP_PCT=2.0  PM_TP_PNL_PCT=3.0 (0 = off)
  PM_TP_FIRST_NIGHT_ONLY=true   only positions bought on the previous trading day (what was tested)
  PM_TP_PLACE_FROM_MIN=245  PM_TP_PLACE_UNTIL_MIN=560   (minutes after midnight ET: 04:05 .. 09:20)
  PM_TP_RESTORE_SECONDS=10  PM_TP_POLL_SECONDS=20  PM_TP_MIN_PRICE=3
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import TrailingStopOrderRequest

from backend.execution import (
    ALPACA_BASE_URL,
    TRAIL_PCT,
    _cancel_all_open_sell_orders,
    _cancel_order_by_id,
    _get_open_orders_for_symbol,
    _get_open_positions,
    _post_order,
    _request,
    open_stops,
    place_trailing_stop_sell,
)
from backend import news_watch as _nw
from backend.news_watch import _regular_exit
from backend.services.trade_ledger_service import close_entry, load_ledger, save_ledger, _from_iso

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_STATE: dict[str, dict] = {}
_FAST = [False]


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


def _prev_business_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _open_entry(ledger: dict, sym: str):
    es = ledger.get(sym)
    for e in reversed(es if isinstance(es, list) else []):
        if isinstance(e, dict) and e.get("status") == "open":
            return e
    return None


def _take_profit_price(prior_close: float, entry: float) -> float:
    gap, pnl = _f("PM_TP_GAP_PCT", 2.0) / 100.0, _f("PM_TP_PNL_PCT", 3.0) / 100.0
    thr = max(prior_close * (1.0 + gap), entry)
    if pnl > 0:
        thr = min(thr, entry * (1.0 + pnl))
    return round(thr, 2)


def _save_meta(sym: str, value) -> None:
    led = load_ledger()
    e = _open_entry(led, sym)
    if e is None:
        return
    md = e.setdefault("metadata", {})
    if value is None:
        md.pop("pm_tp", None)
    else:
        md["pm_tp"] = value
    save_ledger(led)


def _update_meta(sym: str, **kw) -> None:
    led = load_ledger()
    e = _open_entry(led, sym)
    if e is None:
        return
    md = e.setdefault("metadata", {})
    cur = dict(md.get("pm_tp") or {})
    cur.update(kw)
    md["pm_tp"] = cur
    save_ledger(led)


def _reconcile(held: set[str]) -> None:
    led = load_ledger()
    changed = False
    for sym, es in led.items():
        for e in es if isinstance(es, list) else []:
            if isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("pm_tp") and sym not in held:
                close_entry(led, symbol=sym, order_id=None, exit_price=None,
                            reason="premarket_tp_stop" if (e["metadata"]["pm_tp"] or {}).get("stopped") else "premarket_tp")
                logger.warning("pm_tp: %s is out - ledger entry closed (premarket_tp)", sym)
                _STATE.pop(sym, None)
                changed = True
                break
    if changed:
        save_ledger(led)


def _stop_info(orders: list[dict]) -> tuple[float | None, float | None]:
    """highest stop price among the open stop-type sells, and the trailing % if there is one."""
    stop, trail = None, None
    for o in orders:
        if str(o.get("side", "")).lower() != "sell":
            continue
        t = str(o.get("type", "")).lower()
        if t not in ("stop", "stop_limit", "trailing_stop"):
            continue
        try:
            sp = float(o.get("stop_price") or 0)
        except Exception:
            sp = 0.0
        if sp > 0 and (stop is None or sp > stop):
            stop = sp
        try:
            tp = float(o.get("trail_percent") or 0)
        except Exception:
            tp = 0.0
        if t == "trailing_stop" and tp > 0 and (trail is None or tp < trail):
            trail = tp
    return stop, trail


async def _place(sym: str, p: dict, e: dict, dry: bool) -> None:
    qty = float(p.get("qty") or 0)
    entry = float(p.get("avg_entry_price") or e.get("entry_price") or 0)
    prior = float(p.get("lastday_price") or 0)
    if qty <= 0 or entry <= 0 or prior <= 0:
        return
    px = _take_profit_price(prior, entry)
    if px <= 0 or px < _f("PM_TP_MIN_PRICE", 3.0):
        return
    orders = await _get_open_orders_for_symbol(sym)
    for o in orders:  # someone else's exit is already working on this position
        if str(o.get("side", "")).lower() == "sell" and str(o.get("type", "")).lower() in ("limit", "market", "stop_limit"):
            return
    stop, trail = _stop_info(orders)
    qs = str(int(qty)) if float(qty).is_integer() else str(qty)
    logger.warning("PM TP %s qty=%s limit sell %.2f (prior close %.2f, entry %.2f, %+.2f%% on entry) old stop=%s trail=%s%s",
                   sym, qs, px, prior, entry, (px / entry - 1) * 100, stop, trail, " [DRY RUN]" if dry else "")
    if dry:
        _STATE[sym] = {"dry": True}
        return
    # save the plan BEFORE touching the stops, so a restart in between still restores them
    _save_meta(sym, {"price": px, "stop": stop, "trail": trail, "qty": qty, "ts": time.time(), "restored": False})
    await _cancel_all_open_sell_orders(sym)
    left = [o for o in await _get_open_orders_for_symbol(sym) if str(o.get("side", "")).lower() == "sell"]
    if left:
        logger.warning("pm_tp: %s still has sell orders after cancel - not placing", sym)
        await _restore(sym, qty, {"stop": stop, "trail": trail})
        _save_meta(sym, None)
        return
    res = await _post_order({"symbol": sym, "qty": qs, "side": "sell", "type": "limit", "limit_price": str(px),
                             "time_in_force": "day", "extended_hours": True})
    if res and res.get("id"):
        _STATE[sym] = {"id": res.get("id"), "plan": {"stop": stop, "trail": trail, "qty": qty, "order": res.get("id")}}
        _update_meta(sym, order=res.get("id"))
        logger.warning("PM TP %s order placed id=%s", sym, res.get("id"))
    else:
        logger.error("PM TP %s order NOT placed - putting protection back", sym)
        await _restore(sym, qty, {"stop": stop, "trail": trail})
        _save_meta(sym, None)


_SELL_DONE = {"filled", "canceled", "rejected", "expired", "done_for_day", "replaced"}


_BULK: dict = {"t": 0.0, "data": None, "lock": None, "blocked_until": 0.0}


async def _bulk_open_sells(max_age: float = 1.0) -> dict | None:
    """Every open SELL order on the account keyed by symbol, from ONE request that all the
    restores running at the same moment share (at most one fetch per max_age seconds).
    Returns None when the orders could not be read (rate limit etc.) - callers must treat that
    as 'unknown', never as 'no orders': the old per-symbol lookup swallowed a 429 as an empty
    list, which made the bot place a stop on shares another order was already holding."""
    if _BULK["lock"] is None:
        _BULK["lock"] = asyncio.Lock()
    async with _BULK["lock"]:
        now = time.time()
        if _BULK["data"] is not None and now - _BULK["t"] < max_age:
            return _BULK["data"]
        if now < _BULK["blocked_until"]:
            return None
        try:
            resp = await _request("GET", f"{ALPACA_BASE_URL}/v2/orders",
                                  params={"status": "open", "limit": 500, "nested": "false"})
            if resp.status_code != 200:
                if resp.status_code == 429:
                    _BULK["blocked_until"] = time.time() + 2.0
                logger.warning("PM TP: could not read open orders (%s) - will retry", resp.status_code)
                _BULK["data"] = None
                return None
            rows = resp.json()
            out: dict[str, list[dict]] = {}
            for o in rows if isinstance(rows, list) else []:
                if str(o.get("side", "")).lower() == "sell" and str(o.get("status", "")).lower() not in _SELL_DONE:
                    out.setdefault(str(o.get("symbol")), []).append(o)
            _BULK["data"], _BULK["t"] = out, time.time()
            return out
        except Exception:
            logger.exception("PM TP: open-order read failed")
            _BULK["data"] = None
            return None


async def _open_sells(sym: str) -> list[dict] | None:
    snap = await _bulk_open_sells()
    return None if snap is None else list(snap.get(sym, []))


async def _restore(sym: str, qty: float, plan: dict, cur: float | None = None) -> bool:
    """Take the take-profit limit off and put protection back - touching ONLY our own order.

    Anything else already working on the position (the exit timer's market sell, a stop that
    something else placed) is left alone: the position is being handled, we must not cancel it.
    Uses one shared order list (see _bulk_open_sells) and a single order post, so restoring 30
    positions at the open costs ~70 requests instead of ~300 and no longer trips Alpaca's
    200/min limit."""
    mine = plan.get("order") or (_STATE.get(sym) or {}).get("id")
    if mine:
        await _cancel_order_by_id(str(mine))
    else:  # no id on record (ledger copy lost): cancel only extended-hours LIMIT sells
        for o in (await _open_sells(sym)) or []:
            if str(o.get("type", "")).lower() == "limit":
                await _cancel_order_by_id(str(o.get("id")))
    others = None
    for _ in range(12):  # wait (up to ~12 s) until our order is really gone
        cur_orders = await _open_sells(sym)
        if cur_orders is not None:
            gone = (not any(str(o.get("id")) == str(mine) for o in cur_orders)) if mine else (
                not any(str(o.get("type", "")).lower() == "limit" for o in cur_orders))
            if gone:
                others = [o for o in cur_orders if str(o.get("id")) != str(mine)]
                break
        await asyncio.sleep(1.0)
    if others is None:
        logger.warning("PM TP %s: cancel not confirmed / orders unreadable (rate limit?) - will retry", sym)
        return False
    if others:
        logger.warning("PM TP %s: another sell order is already working (%s) - leaving it, no stop added", sym,
                       ",".join(sorted({str(o.get("type", "?")) for o in others})))
        return True
    stop, old_trail = plan.get("stop"), plan.get("trail")
    if cur is None:
        try:
            for p in await _get_open_positions():
                if str(p.get("symbol")) == sym:
                    cur = float(p.get("current_price") or 0)
                    qty = float(p.get("qty") or qty)
        except Exception:
            pass
    if qty <= 0:
        return True
    if stop and cur and cur <= stop:
        logger.warning("PM TP %s price %.2f is at/under the old stop %.2f - selling at market (what the stop would have done)", sym, cur, stop)
        plan["stopped"] = True
        return bool(await _regular_exit(sym, qty))
    base = float(old_trail or TRAIL_PCT)
    trail = base
    if stop and cur and cur > stop:
        trail = max(0.5, min(base, (cur - stop) / cur * 100.0))
    # One direct post: the position and the absence of other sell orders were just verified above.
    res = await _post_order(TrailingStopOrderRequest(
        symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, trail_percent=float(trail)))
    tid = res.get("id") if isinstance(res, dict) else None
    if tid:
        open_stops.setdefault(sym, {})["trail"] = str(tid)
        _BULK["t"] = 0.0  # force the next read to include the stop we just added
        logger.warning("PM TP %s protection restored: trailing stop %.2f%% id=%s (old stop %s)", sym, trail, tid, stop)
        return True
    logger.error("PM TP %s could not restore the trailing stop - will retry", sym)
    return False


async def _pass() -> None:
    now = _now()
    if now.weekday() >= 5:
        return
    sec = now.hour * 3600 + now.minute * 60 + now.second
    mins = sec // 60
    open_s = 9 * 3600 + 1800
    dry = _on("PM_TP_DRY_RUN", False)
    led = load_ledger()
    pending = [s for s, es in led.items() if isinstance(es, list) and any(
        isinstance(e, dict) and e.get("status") == "open" and (e.get("metadata") or {}).get("pm_tp")
        and not (e["metadata"]["pm_tp"] or {}).get("restored") for e in es)]
    for s_, st_ in _STATE.items():  # placed this run, even if the ledger copy was overwritten
        if st_.get("plan") and not st_.get("restored") and s_ not in pending:
            pending.append(s_)
    _FAST[0] = bool(pending) and open_s - 60 <= sec <= open_s + 900
    can_place = _f("PM_TP_PLACE_FROM_MIN", 245) <= mins < _f("PM_TP_PLACE_UNTIL_MIN", 560)
    restore_due = sec >= open_s + _f("PM_TP_RESTORE_SECONDS", 10) and sec < 16 * 3600 + 600
    if not can_place and not pending:
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
    if not dry:
        _reconcile(set(pos))
        led = load_ledger()
    # ---- restore protection (all positions at once, a few in parallel) ----
    if restore_due and pending:
        sem = asyncio.Semaphore(6)

        async def _one(sym: str, p: dict, e) -> None:
            plan = dict(((e or {}).get("metadata") or {}).get("pm_tp") or _STATE.get(sym, {}).get("plan") or {})
            async with sem:
                try:
                    ok = await _restore(sym, float(p.get("qty") or 0), plan, float(p.get("current_price") or 0) or None)
                except Exception:
                    logger.exception("pm_tp: restore failed for %s", sym)
                    ok = False
            if ok:
                plan["restored"] = True
                _STATE.setdefault(sym, {})["restored"] = True
                if e is not None:
                    _save_meta(sym, plan)

        jobs = []
        for sym in pending:
            p = pos.get(sym)
            e = _open_entry(led, sym)
            if p is None:
                _STATE.pop(sym, None)
                continue
            last = _STATE.get(sym, {}).get("restore_try", 0)
            if last and time.time() - last < 10:
                continue
            _STATE.setdefault(sym, {})["restore_try"] = time.time()
            if dry:
                continue
            jobs.append(_one(sym, p, e))
        if jobs:
            await asyncio.gather(*jobs)
    # ---- place take-profits ----
    if not can_place:
        return
    today = now.date()
    prev = _prev_business_day(today)
    busy = set(getattr(_nw, "_actions", {}) or {})
    for sym, p in pos.items():
        if sym in _STATE or sym in busy:
            continue
        e = _open_entry(led, sym)
        if e is None or (e.get("metadata") or {}).get("pm_exit"):
            continue
        old = (e.get("metadata") or {}).get("pm_tp")
        if old and (not old.get("restored") or datetime.fromtimestamp(float(old.get("ts") or 0), _ET).date() >= today):
            continue
        if str(e.get("side", "")).lower() in ("short", "sell") or (e.get("metadata") or {}).get("short"):
            continue
        t = _from_iso(e.get("created_at"))
        if t is None:
            continue
        d = t.astimezone(_ET).date()
        if d >= today:
            continue  # bought today (pre-market buys belong to pm_quick_exit)
        if _on("PM_TP_FIRST_NIGHT_ONLY", True) and d < prev:
            continue
        try:
            await _place(sym, p, e, dry)
        except Exception:
            logger.exception("pm_tp: placing failed for %s", sym)
        await asyncio.sleep(1.0)


async def monitor_premarket_tp() -> None:
    if not _on("PM_TP_ENABLED", False):
        logger.info("pm_tp off (set PM_TP_ENABLED=true to turn it on)")
        return
    logger.warning("pm_tp started - pre-market take-profit: gap +%.1f%% / +%.1f%% on entry, first night only=%s%s",
                   _f("PM_TP_GAP_PCT", 2.0), _f("PM_TP_PNL_PCT", 3.0), _on("PM_TP_FIRST_NIGHT_ONLY", True),
                   " [DRY RUN]" if _on("PM_TP_DRY_RUN", False) else "")
    poll = max(10.0, _f("PM_TP_POLL_SECONDS", 20))
    while True:
        try:
            await _pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pm_tp pass failed")
        await asyncio.sleep(5.0 if _FAST[0] else poll)
