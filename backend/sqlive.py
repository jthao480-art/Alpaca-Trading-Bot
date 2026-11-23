"""Ares / scoutTiq LIVE exit study: would stops, targets or trailing stops have helped the flags the bots actually logged?

Read-only: places no orders and changes no bot state. Run inside a bot container:  python -m backend.sqlive

It replays every KEPT flag in scouttiq_flags.jsonl (taken or skipped; repeat flags are not counted, as in the spec) on daily bars
(open/high/low/close) under a set of exit rules, entering at the logged flag price, and prints the net result of each rule next to the
plain 'hold N sessions, sell at the close' rule. Daily bars cannot show the order of events inside a day, so every ambiguous case is
resolved against the rule (a stop is assumed to hit before a target in the same bar).

Costs: 0.30% round trip on every trade, plus 0.10% extra slippage when a stop fills at its price (a gap through the stop fills at the open).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

COST = 0.0030
STOP_SLIP = 0.0010
HORIZONS = (3, 5, 7)


# ───────────────────────────── rules ─────────────────────────────
def _rules() -> list[dict]:
    rules = [{"name": "hold (baseline)"}]
    for s in (0.03, 0.05, 0.08, 0.12):
        rules.append({"name": "hard stop -%d%%" % round(s * 100), "stop": s})
    for t in (0.03, 0.05, 0.08):
        rules.append({"name": "trailing stop %d%%" % round(t * 100), "trail": t})
    for p in (0.03, 0.05, 0.08, 0.12):
        rules.append({"name": "take profit +%d%%" % round(p * 100), "tp": p})
    rules.append({"name": "stop -5%% + target +5%%", "stop": 0.05, "tp": 0.05})
    rules.append({"name": "stop -8%% + target +8%%", "stop": 0.08, "tp": 0.08})
    rules.append({"name": "trail 5%% + target +8%%", "trail": 0.05, "tp": 0.08})
    rules.append({"name": "sell first green close", "first_green": True})
    rules.append({"name": "sell flag-day close if >= +0.3%", "eod_green": 0.003})
    rules.append({"name": "sell next open if flag-day close red", "next_open_red": True})
    for r in rules:
        r["name"] = r["name"].replace("%%", "%")
    return rules


RULES = _rules()


def simulate(entry: float, bars: list[tuple[float, float, float, float]], n: int, rule: dict) -> tuple[float, int, str]:
    """bars[k] = (open, high, low, close) for session k after the flag day (k=0 is the flag day itself).

    Returns (exit_price, exit_index, reason). Needs bars[0..n]."""
    stop, trail, tp = rule.get("stop"), rule.get("trail"), rule.get("tp")
    hwm = entry
    for k in range(0, n + 1):
        o, h, l, c = bars[k]
        stop_px = None
        if stop:
            stop_px = entry * (1 - stop)
        if trail:
            tpx = hwm * (1 - trail)
            stop_px = tpx if stop_px is None else max(stop_px, tpx)
        tp_px = entry * (1 + tp) if tp else None
        if k == 0:
            # only the close is usable on the flag day: the day's range may include moves before the flag time
            if stop_px is not None and c <= stop_px:
                return stop_px * (1 - STOP_SLIP), 0, "stop"
            if tp_px is not None and c >= tp_px:
                return tp_px, 0, "target"
            if rule.get("eod_green") is not None and c >= entry * (1 + rule["eod_green"]):
                return c, 0, "eod_green"
            if rule.get("first_green") and c > entry:
                return c, 0, "first_green"
            hwm = max(hwm, c)
        else:
            if rule.get("next_open_red") and k == 1 and bars[0][3] < entry:
                return o, 1, "next_open_red"
            if tp_px is not None and o >= tp_px:
                return o, k, "target_gap"
            if stop_px is not None and o <= stop_px:
                return o, k, "stop_gap"
            if stop_px is not None and l <= stop_px:
                return stop_px * (1 - STOP_SLIP), k, "stop"
            if tp_px is not None and h >= tp_px:
                return tp_px, k, "target"
            if rule.get("first_green") and c > entry:
                return c, k, "first_green"
            hwm = max(hwm, h)
        if k == n:
            return c, k, "time"
    return bars[n][3], n, "time"


# ───────────────────────────── study ─────────────────────────────
def kept_flags(rows: list[dict]) -> list[dict]:
    """Flags the spec counts: everything except repeats within 5 sessions and shadow-mode rows."""
    out = []
    for r in rows:
        if r.get("event") or not r.get("symbol") or not r.get("date"):
            continue
        w = str(r.get("why_not") or "")
        if w.startswith("repeat") or w == "shadow":
            continue
        try:
            if float(r.get("flag_price")) <= 0:
                continue
        except Exception:
            continue
        out.append(r)
    return out


def run_study(flags: list[dict], ohlc: dict, sessions: list[str], universe: list[str], last_complete: str,
              session_offset, split_guard: float = 0.45) -> dict:
    """ohlc = {symbol: {date: (o,h,l,c)}}. Returns {horizon: {"n", "days", "universe", "rules": [rows]}}."""
    out: dict = {}
    for n in HORIZONS:
        items = []
        for f in flags:
            sym, d0 = str(f["symbol"]).upper(), f["date"]
            ex = session_offset(sessions, d0, n)
            if not ex or ex > last_complete:
                continue
            b = ohlc.get(sym) or {}
            idx = [d0]
            d = d0
            for _ in range(n):
                d = session_offset(sessions, d, 1)
                idx.append(d)
            if any(x not in b for x in idx):
                continue
            bars = [b[x] for x in idx]
            entry = float(f["flag_price"])
            if any(abs(bars[k][3] / entry - 1) > split_guard for k in range(len(bars))):
                continue                                     # a split or a bad print: not scored
            items.append((f, entry, bars))
        if not items:
            continue
        days = sorted({f["date"] for f, _, _ in items})
        base_net = {}
        rule_rows = []
        for rule in RULES:
            nets, reasons = [], {}
            for f, entry, bars in items:
                px, k, why = simulate(entry, bars, n, rule)
                net = px / entry - 1 - COST
                nets.append(net)
                reasons[why] = reasons.get(why, 0) + 1
                if rule["name"].startswith("hold"):
                    base_net[id(f)] = net
            rule_rows.append({"rule": rule["name"], "nets": nets, "reasons": reasons})
        # universe reference over the same windows (flag-day close -> exit close)
        ureturns = []
        for dd in days:
            ex = session_offset(sessions, dd, n)
            vals = []
            for u in universe:
                bu = ohlc.get(u)
                if bu and dd in bu and ex in bu and bu[dd][3] > 0:
                    r = bu[ex][3] / bu[dd][3] - 1
                    if abs(r) <= split_guard:
                        vals.append(r)
            if len(vals) >= 20:
                ureturns.append(sum(vals) / len(vals))
        base = rule_rows[0]["nets"]
        for rr in rule_rows:
            nets = rr["nets"]
            diffs = [a - b for a, b in zip(nets, base)]
            rr.update(mean=sum(nets) / len(nets), median=statistics.median(nets), hit=sum(1 for v in nets if v > 0) / len(nets),
                      worst=min(nets), diff=sum(diffs) / len(diffs), better=sum(1 for v in diffs if v > 1e-12) / len(diffs))
        out[n] = {"n": len(items), "days": days, "universe": (sum(ureturns) / len(ureturns)) if ureturns else None, "rules": rule_rows}
    return out


def format_study(res: dict) -> str:
    if not res:
        return "no flags old enough yet (the shortest horizon needs 3 completed sessions after the flag day)"
    lines = []
    for n in sorted(res):
        g = res[n]
        u = "n/a" if g["universe"] is None else "%+.2f%%" % (100 * g["universe"])
        lines.append("")
        lines.append("=== hold up to %d sessions: %d flags on %d flag-days (%s .. %s) | liquid-universe average over the same windows: %s ===" % (
            n, g["n"], len(g["days"]), g["days"][0], g["days"][-1], u))
        lines.append("%-40s %8s %8s %6s %8s %9s %7s   exits" % ("rule", "mean", "median", "hit", "worst", "vs hold", "better"))
        for r in sorted(g["rules"], key=lambda x: -x["mean"]):
            why = ", ".join("%s %d" % (k, v) for k, v in sorted(r["reasons"].items(), key=lambda kv: -kv[1]))
            lines.append("%-40s %+7.2f%% %+7.2f%% %5.0f%% %+7.2f%% %+8.2f%% %6.0f%%   %s" % (
                r["rule"], 100 * r["mean"], 100 * r["median"], 100 * r["hit"], 100 * r["worst"], 100 * r["diff"], 100 * r["better"], why))
    lines.append("")
    lines.append("mean/median/worst = net result per flag after 0.30% cost (stop fills also pay 0.10% slippage). 'vs hold' = average difference to the")
    lines.append("plain hold on the same flags; 'better' = share of flags where the rule beat the plain hold. Daily bars only; a stop is assumed to")
    lines.append("hit before a target in the same bar. Flags on few days in one market regime are NOT enough to choose an exit rule.")
    return "\n".join(lines)


# ───────────────────────────── data + CLI ─────────────────────────────
def fetch_ohlc_sync(symbols: list[str], start: str, end: str, feed: str) -> dict:
    import time
    import httpx
    from backend import scouttiq as sq
    out: dict = {}
    hdr = sq._alpaca_headers()
    with httpx.Client(timeout=60) as c:
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i + 100]
            token = None
            while True:
                params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start, "end": end,
                          "adjustment": "raw", "feed": feed, "limit": 10000}
                if token:
                    params["page_token"] = token
                r = None
                for attempt in range(4):
                    r = c.get(sq._data_url() + "/v2/stocks/bars", params=params, headers=hdr)
                    if r.status_code == 429:
                        time.sleep(5 * (attempt + 1))
                        continue
                    break
                if r is None or r.status_code != 200:
                    raise RuntimeError("bars request failed: %s" % getattr(r, "status_code", None))
                js = r.json()
                for sym, arr in (js.get("bars") or {}).items():
                    d = out.setdefault(sym, {})
                    for b in arr:
                        d[b["t"][:10]] = (float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]))
                token = js.get("next_page_token")
                if not token:
                    break
            time.sleep(0.4)
    return out


def main() -> None:
    import logging
    from backend import scouttiq as sq
    logging.getLogger("httpx").setLevel(logging.WARNING)
    flags = kept_flags(sq.read_flag_rows())
    if not flags:
        print("no flags logged yet")
        return
    now = datetime.now(sq.ET)
    today = now.date().isoformat()
    start = (sq._d(min(f["date"] for f in flags)) - timedelta(days=4)).isoformat()
    cal = sq.fetch_calendar_sync(start, (now.date() + timedelta(days=60)).isoformat())
    if not cal:
        raise SystemExit("could not load the trading calendar")
    if today in cal and (now.hour, now.minute) >= (16, 20):
        last_complete = today
    else:
        last_complete = max([x for x in cal if x < today] or [start])
    base = sq.load_base()
    universe = [r["t"] for r in base["rows"]] if base else []
    syms = sorted({str(f["symbol"]).upper() for f in flags} | set(universe))
    end_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("fetching daily bars for %d symbols (%d kept flags since %s)..." % (len(syms), len(flags), min(f["date"] for f in flags)))
    ohlc = fetch_ohlc_sync(syms, start, end_ts, sq._env("SCOUTTIQ_BARS_FEED", "sip"))
    res = run_study(flags, ohlc, cal, universe, last_complete, sq.session_offset)
    print(format_study(res))
    # the same study per market regime (tags are logged at flag time; flags before tagging began are 'untagged')
    buckets: dict = {}
    for f in flags:
        buckets.setdefault(sq.regime_bucket((f.get("reg") or {}).get("m5")), []).append(f)
    if len([b for b in buckets if b != "untagged"]) >= 1:
        for b in ("stress", "weak", "calm", "untagged"):
            sub = buckets.get(b) or []
            if len(sub) < 12:
                print("\n--- regime %s: %d flags, too few for a table (needs 12) ---" % (b, len(sub)))
                continue
            print("\n################ regime: %s (%d flags) ################" % (b, len(sub)))
            print(format_study(run_study(sub, ohlc, cal, universe, last_complete, sq.session_offset)))


if __name__ == "__main__":
    main()
