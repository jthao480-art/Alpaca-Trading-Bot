"""
After-hours SHADOW LOG - records what an after-hours "buy the biggest movers" rule WOULD have
picked each evening and how those picks did at the next open. It places NO orders and touches
no positions, stops or ledger; it only reads market data and writes one small file.

Why: the weekend market-wide backtest (60 days, after costs) found ONE promising cell - buying
the 3 most-traded after-hours movers (up 2-15% vs the 4:00 pm close) at 17:30 ET and selling at
the next open (+6% a trade). It was positive in both halves, but the newer half was ~flat, only
37% of trades won, and the profit may come from a few big winners (earnings pops). Four weeks of
live, out-of-sample data answers whether it is real, at no risk.

What it does (weekdays, regular 16:00 close days only):
  ~18:20 ET  scan the whole tradable universe with SIP 5-min bars (the free plan's SIP history must
             be >= 15 min old, which is why it runs after the decision times, not at them),
             rebuild what the rule would have seen at 17:00 / 17:30 / 18:00 ET, keep the 5 most-
             traded movers per time and band (2-7% and 2-15%), and append them to ah_shadow.jsonl.
             The price 20 minutes after each decision is stored too (a delayed-data bot could not
             buy any earlier).
  ~16:25 ET  next trading day: fill in each pick's next open and close.
Roughly 80 data calls per evening, paced (AH_SHADOW_PACE_SECONDS) and retried on 429.

Read the results any time (Railway shell, from /app):   python3 -m backend.ah_shadow

Switches (Railway variables, read at start-up):
  AH_SHADOW_ENABLED=true        turn it on (default OFF)
  AH_SHADOW_RUN_MIN=1100        minutes after midnight ET to run the evening scan (1100 = 18:20)
  AH_SHADOW_RESOLVE_MIN=985     minutes after midnight ET to fill in next-day results (985 = 16:25)
  AH_SHADOW_PACE_SECONDS=0.7    gap between data calls
  AH_SHADOW_MIN_DV=25000        extended-hours dollars traded so far (same as the backtest)
  AH_SHADOW_PREV_DV=1000000     that day's regular-session dollar volume (same as the backtest)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
TIMES = [(17, 0), (17, 30), (18, 0)]
BANDS = (("r27", 2.0, 7.0), ("r215", 2.0, 15.0))
KEEP_TOP = 5
SELL_SLIP = 0.10  # % taken off the next-open sell price, same as the backtest
FILLS = (("fill 0.30% above the last print", "p", 0.30),
         ("fill 1.00% above the last print", "p", 1.00),
         ("bought 20 min late (+1.00%)", "p20", 1.00))


def _data_dir() -> Path:
    try:
        from backend import config
        return Path(getattr(config, "DATA_DIR", "."))
    except Exception:
        return Path(os.environ.get("DATA_DIR", "."))


def _path() -> Path:
    return _data_dir() / "ah_shadow.jsonl"


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _on(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------------------------------
# file helpers (one JSON object per line; "marker" lines record that a day was scanned)
# ----------------------------------------------------------------------------------------------
def _load() -> list[dict]:
    p = _path()
    out: list[dict] = []
    if not p.exists():
        return out
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        logger.exception("ah_shadow: could not read %s", p)
    return out


def _save(rows: list[dict]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, p)


def _scanned_days(rows: list[dict]) -> set[str]:
    return {r.get("day") for r in rows if r.get("marker") == "scan"}


# ----------------------------------------------------------------------------------------------
# data access (paced, retries on 429)
# ----------------------------------------------------------------------------------------------
_last_call = [0.0]


async def _get(path: str, params: dict, *, data_api: bool = True, tries: int = 6) -> Optional[Any]:
    from backend.services.auth import alpaca_request_async
    for i in range(tries):
        wait = _f("AH_SHADOW_PACE_SECONDS", 0.7) - (time.monotonic() - _last_call[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            resp = await alpaca_request_async("GET", path, params=params, use_data_api=data_api)
        except Exception:
            await asyncio.sleep(5)
            continue
        code = resp.status_code
        if code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if code == 429:
            await asyncio.sleep(20 + 10 * i)
            continue
        if code in (403, 404, 422):
            return None
        await asyncio.sleep(5)
    return None


def _pt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def _bars(symbols: list[str], tf: str, t0: datetime, t1: datetime, chunk: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), chunk):
        ch = symbols[i:i + chunk]
        tok = None
        for _ in range(40):
            params = {"symbols": ",".join(ch), "timeframe": tf, "start": t0.isoformat(), "end": t1.isoformat(),
                      "feed": "sip", "adjustment": "raw", "limit": 10000}
            if tok:
                params["page_token"] = tok
            r = await _get("/v2/stocks/bars", params)
            if not isinstance(r, dict):
                break
            for s, bl in (r.get("bars") or {}).items():
                out.setdefault(s, []).extend(bl)
            tok = r.get("next_page_token")
            if not tok:
                break
        await asyncio.sleep(0)
    return out


async def _universe() -> list[str]:
    data = await _get("/v2/assets", {"status": "active", "asset_class": "us_equity"}, data_api=False)
    if not isinstance(data, list):
        return []
    ok_ex = ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
    return sorted({a["symbol"] for a in data
                   if a.get("tradable") and str(a.get("exchange", "")).upper() in ok_ex
                   and re.fullmatch(r"[A-Z]{1,5}", a.get("symbol", ""))})


async def _is_normal_day(day: date) -> bool:
    r = await _get("/v2/calendar", {"start": day.isoformat(), "end": day.isoformat()}, data_api=False)
    if not isinstance(r, list) or not r:
        return False
    return str(r[0].get("close", "")) == "16:00" and str(r[0].get("date", "")) == day.isoformat()


# ----------------------------------------------------------------------------------------------
# evening scan
# ----------------------------------------------------------------------------------------------
async def _evening_scan(day: date, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(_ET)
    min_dv = _f("AH_SHADOW_MIN_DV", 25000)
    prev_dv = _f("AH_SHADOW_PREV_DV", 1_000_000)
    end = now - timedelta(minutes=16)  # free SIP history must be >= 15 min old
    syms = await _universe()
    if not syms:
        logger.warning("ah_shadow: could not load the asset list")
        return -1
    t_ref = datetime(day.year, day.month, day.day, 15, 55, tzinfo=_ET)
    t_cut = datetime(day.year, day.month, day.day, 16, 5, tzinfo=_ET)

    # stage 1: 5-min bars for everything, keep names that moved >= 1% at any decision time
    cands: dict[str, dict] = {}
    got_any = 0
    for i in range(0, len(syms), 200):
        raw = await _bars(syms[i:i + 200], "5Min", t_ref, end, 200)
        for s, bl in raw.items():
            got_any += 1
            pts = []
            ref0 = None
            for b in bl:
                t = _pt(b["t"]).astimezone(_ET)
                if t.date() != day:
                    continue
                if t == t_ref:
                    ref0 = float(b["c"])
                if t >= t_cut:
                    pts.append((t, float(b["c"]), float(b["c"]) * float(b["v"]), float(b["h"]), float(b["l"])))
            if not pts or not ref0 or ref0 < 2.0:
                continue
            pts.sort(key=lambda z: z[0])
            per_t = {}
            for (hh, mm) in TIMES:
                Td = datetime(day.year, day.month, day.day, hh, mm, tzinfo=_ET)
                done = [z for z in pts if z[0] + timedelta(minutes=5) <= Td]
                if not done or Td - (done[-1][0] + timedelta(minutes=5)) > timedelta(minutes=30):
                    continue
                last = done[-1]
                if not (1.0 <= 100 * (last[1] / ref0 - 1) <= 17.0):
                    continue
                Tl = Td + timedelta(minutes=20)
                late = [z for z in pts if z[0] + timedelta(minutes=5) <= Tl]
                p20 = late[-1][1] if (Tl <= end and late and Tl - (late[-1][0] + timedelta(minutes=5)) <= timedelta(minutes=30)) else None
                per_t[f"{hh:02d}:{mm:02d}"] = dict(p=last[1], p20=p20, dv=sum(z[2] for z in done),
                                                    rng=100 * (last[3] - last[4]) / last[1])
            if per_t:
                cands[s] = per_t
    if got_any == 0:
        logger.warning("ah_shadow: no SIP bars came back at all (data plan / rate limit?) - will retry later")
        return -1

    # stage 2: the official close + regular-session dollar volume for the candidates
    ref: dict[str, float] = {}
    d0 = datetime(day.year, day.month, day.day, 0, 0, tzinfo=_ET)
    daily = await _bars(sorted(cands), "1Day", d0, end, 100)
    for s, bl in daily.items():
        for b in bl:
            if b["t"][:10] == day.isoformat():
                c = float(b["c"])
                if c >= 2.0 and c * float(b["v"]) >= prev_dv:
                    ref[s] = c

    hits: dict[str, list[dict]] = {}
    for s, per_t in cands.items():
        if s not in ref:
            continue
        for tk, v in per_t.items():
            gain = 100 * (v["p"] / ref[s] - 1)
            if 2.0 <= gain <= 15.0 and v["dv"] >= min_dv:
                hits.setdefault(tk, []).append(dict(sym=s, gain=gain, **v))

    # rank by dollars traded within each band, keep the top few
    rows: list[dict] = []
    for tk, lst in hits.items():
        ranked: dict[str, dict[str, int]] = {}
        for key, lo, hi in BANDS:
            inb = sorted([h for h in lst if lo <= h["gain"] < hi + (0.0001 if hi >= 15 else 0)], key=lambda z: -z["dv"])
            for r, h in enumerate(inb[:KEEP_TOP], 1):
                ranked.setdefault(h["sym"], {})[key] = r
        for h in lst:
            if h["sym"] in ranked:
                rows.append(dict(day=day.isoformat(), T=tk, sym=h["sym"], gain=round(h["gain"], 2), p=h["p"], p20=h["p20"],
                                 dv=round(h["dv"]), rng=round(h["rng"], 2), r27=ranked[h["sym"]].get("r27"),
                                 r215=ranked[h["sym"]].get("r215"), news=None, nxd=None, nxo=None, nxc=None))

    # news tag: any headline between 16:00 and the decision time
    wanted = sorted({r["sym"] for r in rows})
    arts: dict[str, list[datetime]] = {}
    ok = bool(wanted)
    t0 = datetime(day.year, day.month, day.day, 16, 0, tzinfo=_ET)
    t1 = datetime(day.year, day.month, day.day, 19, 0, tzinfo=_ET)
    for j in range(0, len(wanted), 25):
        tok = None
        for _ in range(8):
            params = {"symbols": ",".join(wanted[j:j + 25]), "start": t0.isoformat(), "end": t1.isoformat(),
                      "limit": 50, "include_content": "false"}
            if tok:
                params["page_token"] = tok
            r = await _get("/v1beta1/news", params)
            if not isinstance(r, dict):
                ok = False
                break
            for a in r.get("news") or []:
                ts = _pt(a["created_at"]).astimezone(_ET)
                for sy in a.get("symbols") or []:
                    arts.setdefault(sy, []).append(ts)
            tok = r.get("next_page_token")
            if not tok:
                break
    if ok:
        for r in rows:
            hh, mm = [int(x) for x in r["T"].split(":")]
            Td = datetime(day.year, day.month, day.day, hh, mm, tzinfo=_ET)
            r["news"] = any(t <= Td for t in arts.get(r["sym"], []))

    allrows = _load()
    allrows += rows
    allrows.append(dict(marker="scan", day=day.isoformat(), n=len(rows), at=now.isoformat()))
    _save(allrows)

    for tk in ("17:30",):
        top = sorted([r for r in rows if r["T"] == tk and r.get("r215")], key=lambda z: z["r215"])[:3]
        logger.warning("ah_shadow %s: %d picks logged | top 3 at %s: %s", day, len(rows), tk,
                       ", ".join(f"{r['sym']} {r['gain']:+.1f}% ${r['dv'] / 1000:.0f}k" for r in top) or "none")
    return len(rows)


# ----------------------------------------------------------------------------------------------
# next-day results
# ----------------------------------------------------------------------------------------------
async def _resolve(today: date) -> int:
    rows = _load()
    pend = [r for r in rows if not r.get("marker") and r.get("nxo") is None and not r.get("dead")
            and r.get("day", "9999") < today.isoformat()]
    if not pend:
        return 0
    syms = sorted({r["sym"] for r in pend})
    d0 = date.fromisoformat(min(r["day"] for r in pend)) + timedelta(days=1)
    start = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=_ET)
    end = datetime.now(_ET) - timedelta(minutes=16)
    bars = await _bars(syms, "1Day", start, end, 100)
    done = 0
    for r in pend:
        bl = sorted(bars.get(r["sym"], []), key=lambda b: b["t"])
        nb = next((b for b in bl if b["t"][:10] > r["day"]), None)
        if nb:
            r["nxd"], r["nxo"], r["nxc"] = nb["t"][:10], float(nb["o"]), float(nb["c"])
            done += 1
        elif (today - date.fromisoformat(r["day"])).days > 7:
            r["dead"] = True
    if done or any(r.get("dead") for r in pend):
        _save(rows)
    return done


# ----------------------------------------------------------------------------------------------
# background task started from main.py
# ----------------------------------------------------------------------------------------------
async def monitor_ah_shadow() -> None:
    if not _on("AH_SHADOW_ENABLED", False):
        logger.info("ah_shadow off (set AH_SHADOW_ENABLED=true to turn it on)")
        return
    run_min = int(_f("AH_SHADOW_RUN_MIN", 1100))
    res_min = int(_f("AH_SHADOW_RESOLVE_MIN", 985))
    logger.warning("ah_shadow started - evening scan at %02d:%02d ET, next-day results at %02d:%02d ET, file %s (NO orders)",
                   run_min // 60, run_min % 60, res_min // 60, res_min % 60, _path())
    tries: dict[str, int] = {}
    resolved_day: Optional[str] = None
    while True:
        try:
            now = datetime.now(_ET)
            mins = now.hour * 60 + now.minute
            if now.weekday() < 5:
                day = now.date()
                dk = day.isoformat()
                if mins >= res_min and resolved_day != dk:
                    resolved_day = dk
                    n = await _resolve(day)
                    if n:
                        logger.warning("ah_shadow: filled in next-day results for %d picks", n)
                if run_min <= mins <= run_min + 80 and dk not in _scanned_days(_load()) and tries.get(dk, 0) < 3:
                    tries[dk] = tries.get(dk, 0) + 1
                    if await _is_normal_day(day):
                        n = await _evening_scan(day)
                        if n < 0:
                            await asyncio.sleep(300)
                    else:
                        rows = _load()
                        rows.append(dict(marker="scan", day=dk, n=0, at=now.isoformat(), skipped="not a normal 16:00 close day"))
                        _save(rows)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ah_shadow loop failed")
        await asyncio.sleep(60)


# ----------------------------------------------------------------------------------------------
# report:  python3 -m backend.ah_shadow
# ----------------------------------------------------------------------------------------------
def _mean(v: list[float]) -> Optional[float]:
    return sum(v) / len(v) if v else None


def _median(v: list[float]) -> Optional[float]:
    v = sorted(v)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _fx(x: Optional[float], w: int = 7) -> str:
    return f"{x:+{w}.2f}" if x is not None else " " * (w - 1) + "-"


def _ret(r: dict, key: str, slip: float, close: bool = False) -> Optional[float]:
    base = r.get(key)
    px = r.get("nxc" if close else "nxo")
    if not base or not px:
        return None
    return 100 * (px * (1 - SELL_SLIP / 100.0) / (base * (1 + slip / 100.0)) - 1)


def report() -> None:
    rows = _load()
    picks = [r for r in rows if not r.get("marker")]
    days = sorted(_scanned_days(rows))
    done = [r for r in picks if r.get("nxo")]
    print(f"After-hours shadow log: {len(days)} evenings scanned ({days[0] if days else '-'} to {days[-1] if days else '-'}), "
          f"{len(picks)} picks logged, {len(done)} already have next-day results.")
    if not done:
        print("Nothing resolved yet - results fill in at ~16:25 ET on the next trading day.")
        return
    rdays = sorted({r["day"] for r in done})
    cut = rdays[len(rdays) // 2] if len(rdays) > 1 else None
    print(f"Resolved evenings: {len(rdays)}. Halves for A/B = earlier / later evenings (needs >= 2 evenings). "
          f"All returns are % per trade after costs (sell at the next open minus {SELL_SLIP:.2f}%).\n")
    for lab, key, slip in FILLS:
        for bk, bname in (("r27", "up 2-7%"), ("r215", "up 2-15%")):
            print(f"=== {lab} | {bname} | top-N most-traded per time, sold at the next open ===")
            print(f"{'time / top-N':<18}{'n':>5}{'days':>5} |{'mean%':>8}{'median':>8}{'ex-top3':>9} | {'win%':>5} | "
                  f"{'early':>7}{'late':>7} | {'close%':>7} | {'with news':>9}{'no news':>8}")
            for (hh, mm) in TIMES:
                tk = f"{hh:02d}:{mm:02d}"
                for topn in (1, 3, 5):
                    sel = [r for r in done if r["T"] == tk and r.get(bk) and r[bk] <= topn]
                    v = [(r, _ret(r, key, slip)) for r in sel]
                    v = [(r, x) for r, x in v if x is not None]
                    if not v:
                        continue
                    xs = [x for _, x in v]
                    ex3 = sorted(xs, reverse=True)[3:]
                    ea = [x for r, x in v if cut and r["day"] < cut]
                    la = [x for r, x in v if cut and r["day"] >= cut]
                    cl = [c for c in (_ret(r, key, slip, True) for r, _ in v) if c is not None]
                    nw = [x for r, x in v if r.get("news") is True]
                    nn = [x for r, x in v if r.get("news") is False]
                    win = 100 * sum(1 for x in xs if x > 0) / len(xs)
                    print(f"{tk + ' top ' + str(topn):<18}{len(xs):>5}{len({r['day'] for r, _ in v}):>5} |{_fx(_mean(xs), 8)}{_fx(_median(xs), 8)}"
                          f"{_fx(_mean(ex3), 9)} | {win:>4.0f}% |{_fx(_mean(ea))}{_fx(_mean(la))} | {_fx(_mean(cl))} |"
                          f"{_fx(_mean(nw), 9)}{_fx(_mean(nn), 8)}")
            print()
    print("The backtest's promising row was 17:30, top 3, up 2-15%. It only counts as confirmed if, with ~20+ evenings, it is positive "
          "overall, positive without its 3 best trades, positive in both halves, and still positive in the 'bought 20 min late' table.")
    print("\nLatest picks (17:30, top 3, up 2-15%):")
    last = sorted([r for r in picks if r["T"] == "17:30" and r.get("r215") and r["r215"] <= 3], key=lambda r: (r["day"], r["r215"]))[-15:]
    for r in last:
        res = _ret(r, "p", 1.0)
        print(f"  {r['day']} {r['sym']:<6} up {r['gain']:+5.1f}% | ${r['dv'] / 1000:>7,.0f}k | " +
              (f"next open {res:+6.2f}%" if res is not None else "waiting for next open") + ("  (news)" if r.get("news") else ""))


if __name__ == "__main__":
    report()
