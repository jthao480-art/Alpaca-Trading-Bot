"""scoutTiq exit study (historical replay on daily bars). Run on a bot container: needs backend.scouttiq, the cached SEC file,
Alpaca keys and SEC_USER_AGENT. Read-only: places no orders and changes no bot files (writes one cache file under /tmp).

What it does
  1. Rebuilds the scoutTiq nightly base for every past formation date F (liquid, c2, c3, b, l) point-in-time from the cached SEC facts.
  2. Replays the live rule for session S = F+1 on that day's CLOSE (daily data cannot see intraday prices), with the 85% valid rule,
     c1 = lowest 10% of r5, flag when c1+c2+c3 >= 2, no repeat flag within 5 sessions.
  3. Enters at the close of S and tries a menu of exits on the following daily bars. Cost 0.30% round trip on every trade,
     plus 0.10% extra on stop/trailing fills. Intraday ordering is unknown, so stops are tested before highs update (conservative).
Caveats printed at the end. Statistics: day-clustered paired differences vs the 5-session hold, older/newer halves.
"""
import os, sys, json, math, time, statistics as st
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict, Counter
import numpy as np

COST = 0.30            # % round trip, every trade
STOP_SLIP = 0.10       # % extra on stop / trailing fills
START = os.environ.get("START", "2025-01-02")
MAXH = 10              # longest hold tested
LOOKBACK_BARS_DAYS = 75


def say(*a):
    print(*a)
    sys.stdout.flush()


# ───────────────────────── exit simulator (pure) ─────────────────────────
def sim_exit(E, bars, hold=5, stop=None, trail=None, target=None, recover=None, cwin=None, b=None):
    """E entry price (close of the entry session). bars = [(o,h,l,c)] for sessions after entry, len >= hold.
    Returns (net_pct, exit_day (1-based), reason). Costs are included."""
    sl = E * (1 - stop / 100.0) if stop else None
    tgt = E * (1 + target / 100.0) if target else None
    if recover is not None and b is not None and b > E:
        tgt = E + recover * (b - E)
    hwm = E
    for d in range(1, hold + 1):
        o, h, l, c = bars[d - 1]
        if sl is not None:
            if o <= sl:
                return ((o * (1 - STOP_SLIP / 100.0)) / E - 1) * 100 - COST, d, "stop_gap"
            if l <= sl:
                return ((sl * (1 - STOP_SLIP / 100.0)) / E - 1) * 100 - COST, d, "stop"
        if trail:
            lv = hwm * (1 - trail / 100.0)
            if o <= lv:
                return ((o * (1 - STOP_SLIP / 100.0)) / E - 1) * 100 - COST, d, "trail_gap"
            if l <= lv:
                return ((lv * (1 - STOP_SLIP / 100.0)) / E - 1) * 100 - COST, d, "trail"
        if tgt is not None:
            if o >= tgt:
                return (o / E - 1) * 100 - COST, d, "target_gap"
            if h >= tgt:
                return (tgt / E - 1) * 100 - COST, d, "target"
        if cwin is not None and c >= E * (1 + cwin / 100.0):
            return (c / E - 1) * 100 - COST, d, "close_win"
        hwm = max(hwm, h)
    return (bars[hold - 1][3] / E - 1) * 100 - COST, hold, "time"


RULES = []   # (name, kwargs)
RAW_LOG = []  # (session, symbol) of every raw flag, used by the self-check


def _build_rules():
    R = [("HOLD 5 (current rule)", dict(hold=5))]
    for h in (1, 2, 3, 4, 6, 7, 8, 10):
        R.append(("hold %d sessions" % h, dict(hold=h)))
    for s in (3, 5, 7, 10, 15):
        R.append(("hard stop %d%% + hold 5" % s, dict(hold=5, stop=s)))
    for t in (5, 8, 10, 15):
        R.append(("trailing %d%% + hold 5" % t, dict(hold=5, trail=t)))
    for p in (3, 5, 8, 12):
        R.append(("target +%d%% + hold 5" % p, dict(hold=5, target=p)))
    for f in (0.5, 1.0):
        R.append(("recover %d%% of drop to base + hold 5" % int(f * 100), dict(hold=5, recover=f)))
    for p in (0, 2, 4):
        R.append(("first close >= +%d%% + hold 5" % p, dict(hold=5, cwin=p)))
    R.append(("stop 8% + target 8% + hold 5", dict(hold=5, stop=8, target=8)))
    R.append(("stop 10% + hold 10", dict(hold=10, stop=10)))
    return R


# ───────────────────────── data ─────────────────────────
def fetch_ohlcv(sq, symbols, start, end, feed, sink):
    """Calls sink(symbol, [(date,o,h,l,c,v),...]) after each batch of 100 symbols; keeps nothing itself."""
    import httpx
    hdr = sq._alpaca_headers()
    url = sq._data_url() + "/v2/stocks/bars"
    n_req = 0
    with httpx.Client(timeout=90) as c:
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i + 100]
            token = None
            part = {}
            while True:
                params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start, "end": end,
                          "adjustment": "raw", "feed": feed, "limit": 10000}
                if token:
                    params["page_token"] = token
                r = None
                for attempt in range(5):
                    time.sleep(0.35)
                    r = c.get(url, params=params, headers=hdr)
                    n_req += 1
                    if r.status_code == 429:
                        time.sleep(6 * (attempt + 1))
                        continue
                    break
                if r is None or r.status_code != 200:
                    raise RuntimeError("bars request failed: %s %s" % (getattr(r, "status_code", None), getattr(r, "text", "")[:200]))
                js = r.json()
                for sym, arr in (js.get("bars") or {}).items():
                    part.setdefault(sym, []).extend((b["t"][:10], float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])) for b in arr)
                token = js.get("next_page_token")
                if not token:
                    break
            for sym, rows in part.items():
                sink(sym, rows)
            if (i // 100) % 5 == 0:
                say("  bars fetched for %d/%d symbols (%d requests)" % (min(i + 100, len(symbols)), len(symbols), n_req))


def load_data(sq):
    today = datetime.now(sq.ET).date()
    cal = sq.fetch_calendar_sync((date.fromisoformat(START) - timedelta(days=LOOKBACK_BARS_DAYS)).isoformat(), (today + timedelta(days=15)).isoformat())
    if not cal:
        raise RuntimeError("no calendar")
    last_done = max(s for s in cal if s < today.isoformat())              # never use today's bar
    sessions = [s for s in cal if s <= last_done]
    store = sq._read_json(sq.SEC_PATH(), {})
    if not store:
        raise RuntimeError("SEC cache %s is empty: run the scoutTiq build first" % sq.SEC_PATH())
    sec = sq.SecClient()
    r = sec.get("https://www.sec.gov/files/company_tickers.json")
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in r.json().values()}
    syms = sorted(t for t, c in t2c.items() if t.isalpha() and len(t) <= 5 and c in store and store[c].get("eps") and store[c].get("d202"))
    say("symbols with cached EPS + report dates: %d" % len(syms))
    idx = {s_: i for i, s_ in enumerate(sessions)}
    pos = {s_: i for i, s_ in enumerate(syms)}
    nd, ns = len(sessions), len(syms)
    cache = "/tmp/sqexit_bars.npz"
    arrs = None
    if os.path.exists(cache):
        try:
            z = np.load(cache, allow_pickle=False)
            if str(z["last_done"]) == last_done and int(z["n"]) == ns and z["C"].shape == (ns, nd):
                arrs = tuple(z[k] for k in "OHLCV")
                say("using cached bars from %s" % cache)
        except Exception:
            arrs = None
    if arrs is None:
        O = np.full((ns, nd), np.nan); H = O.copy(); L = O.copy(); C = O.copy(); V = O.copy()

        def sink(sym, rows):
            i = pos.get(sym)
            if i is None:
                return
            for d, o, h, l, c, v in rows:
                j = idx.get(d)
                if j is not None:
                    O[i, j], H[i, j], L[i, j], C[i, j], V[i, j] = o, h, l, c, v

        feed = os.environ.get("SCOUTTIQ_BARS_FEED", "sip")
        fetch_ohlcv(sq, syms, sessions[0], last_done, feed, sink)
        arrs = (O, H, L, C, V)
        try:
            np.savez(cache, O=O, H=H, L=L, C=C, V=V, last_done=np.array(last_done), n=np.array(ns))
        except Exception:
            pass
    return sessions, syms, t2c, store, arrs


# ───────────────────────── replay ─────────────────────────
def replay(sq, sessions, syms, store, t2c, arrs):
    O, H, L, C, V = arrs
    nd, ns = len(sessions), len(syms)
    CV = C * V
    R = np.full_like(C, np.nan)
    R[:, 1:] = C[:, 1:] / C[:, :-1]
    jump = np.zeros_like(C, dtype=bool)
    with np.errstate(invalid="ignore"):
        jump[:, 1:] = (R[:, 1:] <= 0.6) | (R[:, 1:] >= 1.7)
    jcs = np.cumsum(jump, axis=1)
    quarters, d202s = [], []
    for s in syms:
        rec = store[t2c[s]]
        quarters.append(sq.quarterly_eps(rec["eps"]))
        d202s.append(set(rec["d202"]))
    min202 = [min(x) for x in d202s]
    first = next(i for i, s in enumerate(sessions) if s >= START)
    last_i = nd - 1
    trades, stats = [], Counter()
    last_kept = {}
    for fi in range(max(first - 1, 25), last_i - MAXH - 1):
        F, Sx = sessions[fi], fi + 1
        if fi + 6 >= nd:
            break
        target = date.fromisoformat(sessions[fi + 6]) - timedelta(days=364)
        tgt_s = target.isoformat()
        l = C[:, fi]
        b = C[:, fi - 4]
        with np.errstate(invalid="ignore"):
            dv = CV[:, fi - 19:fi + 1].mean(axis=1)
            nosplit = (jcs[:, fi] - jcs[:, fi - 20]) == 0
            ok = (l >= sq.MIN_PRICE) & (dv >= sq.MIN_DV20) & (b > 0) & nosplit
        M = []
        for i in np.nonzero(ok)[0]:
            if min202[i] > F:
                continue
            p = sq.ttm_pair(quarters[i], F)
            if not p:
                continue
            M.append((int(i), p[0], p[1], float(dv[i])))
        stats["formation_days"] += 1
        if len(M) < 50:
            stats["skip_small_universe"] += 1
            continue
        dvs = sorted(x[3] for x in M)
        n = len(dvs)
        med = dvs[n // 2] if n % 2 else 0.5 * (dvs[n // 2 - 1] + dvs[n // 2])
        score = {i: (a - bb) / float(l[i]) for i, a, bb, _ in M}
        top_n = max(1, math.ceil(sq.C2_TOP_FRAC * len(M)))
        c2set = {i for i, _ in sorted(score.items(), key=lambda kv: (-kv[1], syms[kv[0]]))[:top_n]}
        rows = [i for i, a, bb, d in M if d >= med]                      # alphabetical (syms is sorted) = base-file order
        cS = C[:, Sx]
        valid = []
        for pos, i in enumerate(rows):
            p = cS[i]
            if not (p > 0) or abs(p / l[i] - 1) > sq.SPLIT_GUARD:
                continue
            valid.append((pos, i, float(p), float(p / b[i] - 1)))
        if len(valid) < sq.MIN_VALID_FRAC * len(rows):
            stats["skip_valid_fraction"] += 1
            continue
        k = round(sq.DECILE * len(valid))
        c1 = {v[1] for v in sorted(valid, key=lambda v: (v[3], v[0]))[:k]}
        stats["days_replayed"] += 1
        stats["liquid_names_sum"] += len(rows)
        for pos, i, p, r5 in valid:
            c3 = 1 if tgt_s in d202s[i] else 0
            n1, n2 = (1 if i in c1 else 0), (1 if i in c2set else 0)
            if n1 + n2 + c3 < 2:
                continue
            stats["flags_raw"] += 1
            RAW_LOG.append((sessions[Sx], syms[i]))
            if i in last_kept and Sx - last_kept[i] <= sq.HOLD_SESSIONS:
                stats["flags_repeat_blocked"] += 1
                continue
            bars = []
            good = True
            for d in range(1, MAXH + 1):
                o_, h_, l_, c_ = O[i, Sx + d], H[i, Sx + d], L[i, Sx + d], C[i, Sx + d]
                if not (o_ > 0 and h_ > 0 and l_ > 0 and c_ > 0):
                    good = False
                    break
                bars.append((float(o_), float(h_), float(l_), float(c_)))
            if not good:
                stats["flags_dropped_missing_bars"] += 1
                last_kept[i] = Sx
                continue
            last_kept[i] = Sx
            kind = "".join(t for t, f in (("1", n1), ("2", n2), ("3", c3)) if f)
            trades.append(dict(sym=syms[i], day=sessions[Sx], E=p, b=float(b[i]), kind=kind, bars=bars, r5=r5))
    return trades, stats


# ───────────────────────── statistics ─────────────────────────
def day_t(vals_by_day):
    xs = [st.fmean(v) for v in vals_by_day.values()]
    if len(xs) < 3:
        return float("nan")
    sd = st.pstdev(xs) * math.sqrt(len(xs) / (len(xs) - 1))
    return st.fmean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else float("nan")


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))]


def report(trades, stats, split_date):
    say("\n== 0) REPLAY SUMMARY ==")
    say("formation days: %d | replayed: %d | skipped (<50 matchable): %d | skipped (<85%% valid): %d" % (
        stats["formation_days"], stats["days_replayed"], stats["skip_small_universe"], stats["skip_valid_fraction"]))
    say("average liquid names per day: %.0f" % (stats["liquid_names_sum"] / max(1, stats["days_replayed"])))
    say("raw flags %d | blocked as repeats %d | dropped (missing bars) %d | trades kept %d (%.1f per replayed day)" % (
        stats["flags_raw"], stats["flags_repeat_blocked"], stats["flags_dropped_missing_bars"], len(trades), len(trades) / max(1, stats["days_replayed"])))
    if not trades:
        return
    days = sorted({t["day"] for t in trades})
    say("trade days %s .. %s (%d days)" % (days[0], days[-1], len(days)))
    rules = _build_rules()
    base_name = rules[0][0]
    res = {}
    for name, kw in rules:
        r = []
        for t in trades:
            net, d, why = sim_exit(t["E"], t["bars"], b=t["b"], **kw)
            r.append((net, d, why))
        res[name] = r
    base = [x[0] for x in res[base_name]]
    byday = defaultdict(list)
    for t, x in zip(trades, base):
        byday[t["day"]].append(x)
    say("\n== 1) CURRENT RULE (hold 5 sessions, no stop), net of 0.30% cost ==")
    say("n=%d  mean %+.2f%%  median %+.2f%%  win %.0f%%  5th pct %+.1f%%  worst %+.1f%%  best %+.1f%%  t(day) vs 0: %.2f" % (
        len(base), st.fmean(base), st.median(base), 100 * sum(1 for x in base if x > 0) / len(base), pct(base, .05), min(base), max(base), day_t(byday)))
    old = [x for t, x in zip(trades, base) if t["day"] < split_date]
    new = [x for t, x in zip(trades, base) if t["day"] >= split_date]
    say("older half (<%s): n=%d mean %+.2f%% | newer half: n=%d mean %+.2f%%" % (split_date, len(old), st.fmean(old) if old else float("nan"), len(new), st.fmean(new) if new else float("nan")))
    say("\nby flag type (1 = bottom-decile 5-day drop, 2 = earnings improving, 3 = report due):")
    for kind in sorted({t["kind"] for t in trades}):
        v = [x for t, x in zip(trades, base) if t["kind"] == kind]
        bd = defaultdict(list)
        for t, x in zip(trades, base):
            if t["kind"] == kind:
                bd[t["day"]].append(x)
        say("  %-4s n=%5d  mean %+.2f%%  median %+.2f%%  win %.0f%%  t(day) %.2f" % (kind, len(v), st.fmean(v), st.median(v), 100 * sum(1 for x in v if x > 0) / len(v), day_t(bd)))
    say("\nby quarter (mean net %, n):")
    q = defaultdict(list)
    for t, x in zip(trades, base):
        y, m = int(t["day"][:4]), int(t["day"][5:7])
        q["%d-Q%d" % (y, (m - 1) // 3 + 1)].append(x)
    say("  " + "   ".join("%s %+.2f (%d)" % (k, st.fmean(v), len(v)) for k, v in sorted(q.items())))

    say("\n== 2) HOW FAR DO TRADES FALL BEFORE THE DAY-5 EXIT? (lowest low within 5 sessions vs entry) ==")
    mae = [min(b_[2] for b_ in t["bars"][:5]) / t["E"] - 1 for t in trades]
    for lv in (3, 5, 8, 10, 15, 20):
        sel = [x for m_, x in zip(mae, base) if m_ <= -lv / 100.0]
        say("  dipped to -%2d%% or worse: %5.1f%% of trades | their final result if held: mean %+.2f%%, median %+.2f%% (n=%d)" % (
            lv, 100 * len(sel) / len(base), st.fmean(sel) if sel else float("nan"), st.median(sel) if sel else float("nan"), len(sel)))

    say("\n== 3) EXIT RULES (%d tried). All paired against the 5-session hold on the SAME trades; t is day-clustered ==" % len(rules))
    say("%-38s %5s %8s %8s %6s %8s %7s %9s %9s %7s" % ("rule", "n", "mean%", "median%", "win%", "5th pct", "avg day", "vs base%", "t(day)", "halves"))
    for name, kw in rules:
        r = res[name]
        x = [a for a, _, _ in r]
        diff_by_day = defaultdict(list)
        for t, a, b_ in zip(trades, x, base):
            diff_by_day[t["day"]].append(a - b_)
        do = [a - b_ for t, a, b_ in zip(trades, x, base) if t["day"] < split_date]
        dn = [a - b_ for t, a, b_ in zip(trades, x, base) if t["day"] >= split_date]
        halves = "%+.2f/%+.2f" % (st.fmean(do) if do else 0.0, st.fmean(dn) if dn else 0.0)
        dm = st.fmean(a - b_ for a, b_ in zip(x, base))
        tt = day_t(diff_by_day) if name != base_name else float("nan")
        say("%-38s %5d %+8.2f %+8.2f %5.0f%% %+8.1f %7.1f %+9.2f %9.2f %13s" % (
            name, len(x), st.fmean(x), st.median(x), 100 * sum(1 for a in x if a > 0) / len(x), pct(x, .05), st.fmean(d for _, d, _ in r), dm, tt, halves))
    say("\nexit reasons for the stops/targets (share of trades):")
    for name, kw in rules:
        if any(k in kw for k in ("stop", "trail", "target", "recover", "cwin")):
            c = Counter(w for _, _, w in res[name])
            say("  %-38s %s" % (name, "  ".join("%s %.0f%%" % (k.replace("_gap", " (gap)"), 100 * v / len(trades)) for k, v in c.most_common())))
    say("\nREAD: 'vs base%' is the average per-trade gain over holding 5 sessions (net of the same costs). Treat a rule as real only if t(day) >= 2,")
    say("both halves are positive, and its neighbours (similar stop/target widths) also beat the base. %d rules were tried, so one good-looking row is luck." % len(rules))
    say("CAVEATS: (a) tickers are today's tradable list, so delisted losers are missing (survivorship bias flatters buying losers);")
    say("(b) entries use the close of the flag session, the live bot flags intraday at a different price; (c) the order of high/low inside a day is unknown;")
    say("(d) stops at a daily low assume you are filled at the stop (+0.10% slip), real fills in fast drops can be worse; (e) trades are not capped by position limits.")


def main():
    from backend import scouttiq as sq
    t0 = time.time()
    sessions, syms, t2c, store, arrs = load_data(sq)
    say("sessions %s .. %s | bars loaded for %d of %d symbols (%.0fs)" % (sessions[0], sessions[-1], int((~np.isnan(arrs[3]).all(axis=1)).sum()), len(syms), time.time() - t0))
    trades, stats = replay(sq, sessions, syms, store, t2c, arrs)
    say("replay done (%.0fs)" % (time.time() - t0))
    ds = sorted({t["day"] for t in trades})
    split = ds[len(ds) // 2] if ds else "9999"
    report(trades, stats, split)
    say("DONE")


if __name__ == "__main__":
    main()
