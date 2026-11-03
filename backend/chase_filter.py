"""
Entry "chase" filter - skips (or just logs) scan-driven long buys on stocks that already made their move.

Why: the paper ledger (1,686 long trades, -$23.6k, 12 weeks) shows buys that were
  (1) already up >= 2% from the prior close, or
  (2) made >= 60 minutes after the stock first rose 1% above the prior close
lose far more than the rest. Skipping rule 2 alone would have avoided 562 trades that lost
$11.5k combined; it saved money in both halves of the data, in 13 of 13 weeks, and still saved
$5.9k if each skipped trade were replaced by an average one. (The cash ledger, +$8 over 533
trades, showed no consistent effect - so cash should stay in "shadow" mode.)

Modes (Railway variable ENTRY_CHASE_FILTER, read at start-up and on every check):
  off     (default) does nothing.
  shadow  evaluates and LOGS what it would have skipped, but the buy goes ahead.
  on      skips the buy and logs it.
Other switches:
  ENTRY_CHASE_UP_PCT=2.0     rule 1 - skip when price is already this % above the prior close (0 = off)
  ENTRY_CHASE_LEAD_MIN=60    rule 2 - skip when the stock first closed a 5-min bar 1% above the prior
                             close at least this many minutes ago (0 = off)
Regular session only (09:35-15:55 ET); fails OPEN (buys normally) whenever the data is missing.
Applies to the scan-driven long entries in botv3 only (not Tradetiq buys, news buys, SmartTiq/Nexus, shorts).

Every flagged candidate is appended to chase_filter.jsonl in the data folder together with the
quantity it would have bought; a background task fills in what the stock did by that day's close and at
the next open (hypothetical, after 0.10% sell slippage). Read it with:   python3 -m backend.chase_filter

Caveat: the backtest used SIP 5-minute bars; the bot's own bar feed is IEX, which is thinner, so the
live trigger rate may differ a little. That is what the shadow log is for.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
SELL_SLIP = 0.10  # %
_cache: dict[str, tuple[float, Optional[dict]]] = {}
_logged: set[tuple[str, str]] = set()


def _mode() -> str:
    v = str(os.environ.get("ENTRY_CHASE_FILTER", "off")).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return "on"
    return v if v in ("shadow", "on") else "off"


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _path() -> Path:
    try:
        from backend import config
        return Path(getattr(config, "DATA_DIR", ".")) / "chase_filter.jsonl"
    except Exception:
        return Path(os.environ.get("DATA_DIR", ".")) / "chase_filter.jsonl"


def _load() -> list[dict]:
    p = _path()
    out: list[dict] = []
    if not p.exists():
        return out
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        logger.exception("chase_filter: could not read %s", p)
    return out


def _save(rows: list[dict]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, p)


def _append(rec: dict) -> None:
    try:
        with _path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        logger.exception("chase_filter: could not write the log")


def _pt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


# ----------------------------------------------------------------------------------------------
# the check used by botv3
# ----------------------------------------------------------------------------------------------
async def _context(symbol: str, price: float) -> Optional[dict]:
    """prior close, % above it, and minutes since the stock first closed a 5-min bar 1% above it."""
    from backend.services.bars_service import get_bars
    now = datetime.now(_ET)
    bars = await get_bars(symbol, timeframe="5Min", limit=130)
    if not bars:
        return None
    today = now.date()
    prior_close = None
    first_cross: Optional[datetime] = None
    seen_today = 0
    rows = []
    for b in bars:
        try:
            t = _pt(b["t"]).astimezone(_ET)
            rows.append((t, float(b["c"])))
        except Exception:
            continue
    rows.sort(key=lambda z: z[0])
    for t, c in rows:
        if t.date() < today and 570 <= t.hour * 60 + t.minute < 960:
            prior_close = c
    if not prior_close or prior_close <= 0:
        return None
    for t, c in rows:
        if t.date() == today:
            seen_today += 1
            if first_cross is None and c >= prior_close * 1.01:
                first_cross = t + timedelta(minutes=5)
    if seen_today < 3:
        return None
    lead = None
    if first_cross is not None and first_cross <= now:
        lead = (now - first_cross).total_seconds() / 60.0
    return dict(pc=prior_close, runup=100.0 * (price / prior_close - 1.0), lead=lead)


async def should_skip(symbol: str, price: float, qty: float, agent: str = "", score: Any = None) -> bool:
    """True = do not buy (only in mode 'on'). Never raises; fails open."""
    mode = _mode()
    if mode == "off":
        return False
    now = datetime.now(_ET)
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5 or mins < 9 * 60 + 35 or mins > 15 * 60 + 55:
        return False
    key = symbol.upper()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 240:
        ctx = hit[1]
    else:
        try:
            ctx = await _context(key, float(price))
        except Exception:
            logger.exception("chase_filter: context lookup failed for %s (buy allowed)", key)
            ctx = None
        _cache[key] = (time.monotonic(), ctx)
    if not ctx:
        return False
    up_th = _f("ENTRY_CHASE_UP_PCT", 2.0)
    lead_th = _f("ENTRY_CHASE_LEAD_MIN", 60.0)
    runup = 100.0 * (float(price) / ctx["pc"] - 1.0)
    rules = []
    if up_th > 0 and runup >= up_th:
        rules.append("up")
    if lead_th > 0 and ctx["lead"] is not None and ctx["lead"] >= lead_th:
        rules.append("late")
    if not rules:
        return False
    logger.warning("chase_filter[%s] %s price=%.2f prior_close=%.2f up=%+.2f%% first_rose_1pct=%s min ago -> rule(s) %s%s",
                   mode, key, price, ctx["pc"], runup,
                   "%.0f" % ctx["lead"] if ctx["lead"] is not None else "n/a", "+".join(rules),
                   " - SKIPPED" if mode == "on" else " - buying anyway (shadow)")
    k = (key, now.date().isoformat())
    if k not in _logged:
        _logged.add(k)
        _append(dict(day=now.date().isoformat(), at=now.isoformat(), sym=key, price=float(price), qty=float(qty), pc=ctx["pc"],
                     up=round(runup, 3), lead=(round(ctx["lead"], 1) if ctx["lead"] is not None else None),
                     rules="+".join(rules), mode=mode, agent=agent, score=score, close=None, nxo=None))
    return mode == "on"


# ----------------------------------------------------------------------------------------------
# outcomes (what the flagged stocks did) - background task
# ----------------------------------------------------------------------------------------------
_last_call = [0.0]


async def _get(path: str, params: dict) -> Optional[Any]:
    from backend.services.auth import alpaca_request_async
    for i in range(5):
        wait = 0.8 - (time.monotonic() - _last_call[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            resp = await alpaca_request_async("GET", path, params=params, use_data_api=True)
        except Exception:
            await asyncio.sleep(5)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if resp.status_code == 429:
            await asyncio.sleep(20 + 10 * i)
            continue
        if resp.status_code in (403, 404, 422):
            return None
        await asyncio.sleep(5)
    return None


async def _resolve(today: date) -> int:
    rows = _load()
    pend = [r for r in rows if (r.get("close") is None or r.get("nxo") is None) and r.get("day")]
    pend = [r for r in pend if r["day"] <= today.isoformat()]
    if not pend:
        return 0
    syms = sorted({r["sym"] for r in pend})
    d0 = date.fromisoformat(min(r["day"] for r in pend))
    start = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=_ET)
    end = datetime.now(_ET) - timedelta(minutes=16)
    bars: dict[str, list[dict]] = {}
    for i in range(0, len(syms), 100):
        tok = None
        for _ in range(10):
            params = {"symbols": ",".join(syms[i:i + 100]), "timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(),
                      "feed": "sip", "adjustment": "raw", "limit": 10000}
            if tok:
                params["page_token"] = tok
            r = await _get("/v2/stocks/bars", params)
            if not isinstance(r, dict):
                break
            for s, bl in (r.get("bars") or {}).items():
                bars.setdefault(s, []).extend(bl)
            tok = r.get("next_page_token")
            if not tok:
                break
    n = 0
    for r in pend:
        bl = sorted(bars.get(r["sym"], []), key=lambda b: b["t"])
        if r.get("close") is None:
            same = next((b for b in bl if b["t"][:10] == r["day"]), None)
            _nm = datetime.now(_ET).hour * 60 + datetime.now(_ET).minute
            if same and (r["day"] < today.isoformat() or _nm >= 16 * 60 + 20):
                r["close"] = float(same["c"])
                n += 1
        if r.get("nxo") is None:
            nb = next((b for b in bl if b["t"][:10] > r["day"]), None)
            if nb:
                r["nxo"] = float(nb["o"])
                n += 1
    if n:
        _save(rows)
    return n


async def monitor_chase_outcomes() -> None:
    if _mode() == "off":
        logger.info("chase_filter off (set ENTRY_CHASE_FILTER=shadow or on)")
        return
    logger.warning("chase_filter started in '%s' mode - up>=%.1f%% / first rose 1%% >= %.0f min ago; log %s",
                   _mode(), _f("ENTRY_CHASE_UP_PCT", 2.0), _f("ENTRY_CHASE_LEAD_MIN", 60.0), _path())
    done_day: Optional[str] = None
    while True:
        try:
            now = datetime.now(_ET)
            mins = now.hour * 60 + now.minute
            if now.weekday() < 5 and mins >= 16 * 60 + 25 and done_day != now.date().isoformat():
                done_day = now.date().isoformat()
                n = await _resolve(now.date())
                if n:
                    logger.warning("chase_filter: filled in %d outcome value(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chase_filter outcome task failed")
        await asyncio.sleep(120)


# ----------------------------------------------------------------------------------------------
# report:  python3 -m backend.chase_filter
# ----------------------------------------------------------------------------------------------
def _mean(v: list[float]) -> Optional[float]:
    return sum(v) / len(v) if v else None


def _fx(x: Optional[float], w: int = 8) -> str:
    return f"{x:+{w}.2f}" if x is not None else " " * (w - 1) + "-"


def report() -> None:
    rows = _load()
    if not rows:
        print("No candidates logged yet.")
        return
    days = sorted({r["day"] for r in rows})
    print(f"Chase filter log: {len(rows)} flagged candidates over {len(days)} day(s), {days[0]} to {days[-1]}.")
    print("'to close' / 'to next open' = what a buy at the logged price would have returned (minus %.2f%% sell slippage); '$' uses the quantity the bot wanted to buy.\n" % SELL_SLIP)
    print(f"{'mode / rule':<22}{'n':>5}{'days':>5} | {'to close%':>10}{'$':>9} | {'to next open%':>14}{'$':>9} | {'win@open':>8}")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{r.get('mode')} / {r.get('rules')}", []).append(r)
        groups.setdefault(f"{r.get('mode')} / ALL", []).append(r)
    for k in sorted(groups):
        v = groups[k]
        c = [(100 * (r["close"] * (1 - SELL_SLIP / 100) / r["price"] - 1), r["qty"] * (r["close"] * (1 - SELL_SLIP / 100) - r["price"])) for r in v if r.get("close")]
        o = [(100 * (r["nxo"] * (1 - SELL_SLIP / 100) / r["price"] - 1), r["qty"] * (r["nxo"] * (1 - SELL_SLIP / 100) - r["price"])) for r in v if r.get("nxo")]
        win = 100 * sum(1 for x, _ in o if x > 0) / len(o) if o else None
        print(f"{k:<22}{len(v):>5}{len({r['day'] for r in v}):>5} | {_fx(_mean([x for x, _ in c]), 10)}{sum(d for _, d in c):>+9.0f} | "
              f"{_fx(_mean([x for x, _ in o]), 14)}{sum(d for _, d in o):>+9.0f} | {(f'{win:.0f}%' if win is not None else '-'):>8}")
    print("\nBacktest reference: flagged buys averaged about -0.9% a trade in the paper ledger (real exits). In 'on' mode the stocks were NOT bought, "
          "so a negative number here is money saved; in 'shadow' mode they were bought and the number is roughly what they did.")


if __name__ == "__main__":
    report()
