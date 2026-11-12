"""scoutTiq: nightly base builder + live flag check + 5-session hold, as a bot entry agent.

OFF unless SCOUTTIQ_MODE is "shadow" (log flags only) or "on" (trade them).

The rule (frozen design, per the author's spec)
  nightly: Liquid (20d $ volume >= median of matchable names), c2 (earnings improving, top 20%),
           c3 (own predicted report date lands on the 6th session after the formation row),
           b = raw close 4 sessions before the last close, l = last raw close.
  live:    r5 = price / b - 1 over liquid names with a valid price; skip the cycle if <85% valid
           or the base is >4 days old; c1 = lowest 10% of r5; flag when c1 + c2 + c3 >= 2.
           Version A every ~15 min (full), Version B every ~5 min (names with c2 or c3 only,
           using the cutoff from the last full cycle if <= 20 min old).
  hold:    enter at the flag, exit at the close 5 sessions after the entry session, no intraday stop.

Everything here is data-driven from files under DATA_DIR:
  scouttiq_base.json    tonight's base (rows: t, c2, c3, b, l)            <- written by build_base()
  scouttiq_sec.json     per-company EPS facts + Item 2.02 dates (SEC)     <- refreshed by build_base()
  scouttiq_state.json   open scoutTiq positions, repeat-flag memory, counters
  scouttiq_flags.jsonl  every flag ever raised (taken or not), one JSON per line
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# True while the scoutTiq agent itself is placing an order: lets it through the entry/hold guard.
SCOUTTIQ_CTX: contextvars.ContextVar = contextvars.ContextVar("scouttiq_ctx", default=False)


# ───────────────────────────── config ─────────────────────────────
def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def _env_f(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except Exception:
        return default


def mode() -> str:
    m = _env("SCOUTTIQ_MODE", "off").lower()
    return m if m in ("off", "shadow", "on") else "off"


def only_mode() -> bool:
    """SCOUTTIQ_ONLY=true: every other entry path (and new shorts) is refused at the order chokepoint."""
    return _env("SCOUTTIQ_ONLY", "false").lower() in ("1", "true", "yes", "on")


def data_dir() -> Path:
    try:
        from backend import config
        return Path(getattr(config, "DATA_DIR", ".") or ".")
    except Exception:
        return Path(_env("DATA_DIR", "."))


BASE_PATH = lambda: Path(_env("SCOUTTIQ_BASE_PATH", "")) if _env("SCOUTTIQ_BASE_PATH") else data_dir() / "scouttiq_base.json"
SEC_PATH = lambda: data_dir() / "scouttiq_sec.json"
STATE_PATH = lambda: data_dir() / "scouttiq_state.json"
FLAGS_PATH = lambda: data_dir() / "scouttiq_flags.jsonl"

DECILE = 0.10
SPLIT_GUARD = 0.45
MIN_VALID_FRAC = 0.85
MAX_BASE_AGE_DAYS = 4
HOLD_SESSIONS = 5
REPORT_LEAD_SESSIONS = 6
C2_TOP_FRAC = 0.20
MIN_PRICE = 5.0
MIN_DV20 = 10_000_000.0


# ───────────────────────────── the live rule (verbatim from the spec) ─────────────────────────────
def flags(base_rows, prices, decile=DECILE, split_guard=SPLIT_GUARD, min_frac=MIN_VALID_FRAC):
    # base_rows: [{"t": ticker, "c2": 0/1, "c3": 0/1, "b": base close, "l": last close}]  (liquid names only)
    # prices:    {ticker: live price}
    valid = []
    for i, r in enumerate(base_rows):
        p = prices.get(r["t"])
        if not p or p <= 0 or r["b"] <= 0 or r["l"] <= 0 or abs(p / r["l"] - 1) > split_guard:
            continue
        valid.append((i, r, p, p / r["b"] - 1))
    if not base_rows or len(valid) < min_frac * len(base_rows):
        return []                                    # skipped cycle, never guess
    k = round(decile * len(valid))
    c1_idx = {v[0] for v in sorted(valid, key=lambda v: (v[3], v[0]))[:k]}
    return [r["t"] for i, r, p, r5 in valid if (i in c1_idx) + r["c2"] + r["c3"] >= 2]


def full_cycle(base_rows, prices, decile=DECILE, split_guard=SPLIT_GUARD, min_frac=MIN_VALID_FRAC):
    """Same rule as flags(), but also returns what Version B needs: the c1 cutoff and the r5 of each name.

    Returns dict(skipped=<reason|None>, n_valid, n_total, cutoff, flags, r5).
    flags() is the source of truth; the flag list here is asserted identical to it in the self-test.
    """
    valid = []
    for i, r in enumerate(base_rows):
        p = prices.get(r["t"])
        if not p or p <= 0 or r["b"] <= 0 or r["l"] <= 0 or abs(p / r["l"] - 1) > split_guard:
            continue
        valid.append((i, r, p, p / r["b"] - 1))
    out = {"skipped": None, "n_valid": len(valid), "n_total": len(base_rows), "cutoff": None, "flags": [], "r5": {}}
    if not base_rows:
        out["skipped"] = "empty_base"
        return out
    if len(valid) < min_frac * len(base_rows):
        out["skipped"] = "valid_fraction_%.2f_below_%.2f" % (len(valid) / len(base_rows), min_frac)
        return out
    k = round(decile * len(valid))
    ordered = sorted(valid, key=lambda v: (v[3], v[0]))[:k]
    c1_idx = {v[0] for v in ordered}
    out["cutoff"] = ordered[-1][3] if ordered else None
    out["r5"] = {r["t"]: r5 for i, r, p, r5 in valid}
    out["flags"] = [r["t"] for i, r, p, r5 in valid if (i in c1_idx) + r["c2"] + r["c3"] >= 2]
    return out


def version_b(base_rows, prices, cutoff, split_guard=SPLIT_GUARD):
    """Version B: only names with c2 or c3; flag when r5 <= cutoff. Returns [(ticker, r5, trigger_price)]."""
    if cutoff is None:
        return []
    out = []
    for r in base_rows:
        if not (r["c2"] or r["c3"]):
            continue
        p = prices.get(r["t"])
        if not p or p <= 0 or r["b"] <= 0 or r["l"] <= 0 or abs(p / r["l"] - 1) > split_guard:
            continue
        r5 = p / r["b"] - 1
        if r5 <= cutoff:
            out.append((r["t"], r5, r["b"] * (1 + cutoff)))
    return out


# ───────────────────────────── trading calendar ─────────────────────────────
def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def session_offset(sessions: list[str], start: str, n: int) -> Optional[str]:
    """The session n sessions after `start` (start itself, if it is not a session, counts as the previous one)."""
    if not sessions:
        return None
    idx = None
    for i, s in enumerate(sessions):
        if s <= start:
            idx = i
        else:
            break
    if idx is None:
        return None
    j = idx + n
    return sessions[j] if 0 <= j < len(sessions) else None


def weekday_sessions(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ───────────────────────────── EPS / report-date maths (pure, tested) ─────────────────────────────
def _dur(s: str, e: str) -> int:
    return (_d(e) - _d(s)).days


def quarterly_eps(eps_facts: Iterable) -> list[tuple[str, float, str]]:
    """eps_facts: rows [start, end, val, filed, form]. Returns [(end, discrete_quarter_eps, filed)], oldest first.

    First-reported only: for each (start, end) period the earliest `filed` value wins, later restatements are ignored.
    Q4 (and a missing Q2/Q3) is derived as the cumulative figure minus the earlier cumulative figure, and is dated
    when the cumulative figure was first filed.
    """
    first: dict[tuple[str, str], tuple[float, str]] = {}
    for row in eps_facts:
        try:
            s, e, v, f = row[0], row[1], float(row[2]), row[3]
        except Exception:
            continue
        k = (s, e)
        if k not in first or f < first[k][1]:
            first[k] = (v, f)
    Q: dict[str, tuple[float, str, str]] = {}          # end -> (val, filed, start)
    for (s, e), (v, f) in first.items():
        if 75 <= _dur(s, e) <= 105:
            Q[e] = (v, f, s)
    # Cumulative periods (6m, 9m, 12m) derive any quarter not reported on its own: difference of consecutive
    # cumulatives within one fiscal year, chained from that year's first discrete quarter.
    by_start: dict[str, list[tuple[str, float, str]]] = {}
    for (s, e), (v, f) in first.items():
        if _dur(s, e) > 105:
            by_start.setdefault(s, []).append((e, v, f))
    for s, lst in by_start.items():
        chain = [(e, v, f) for e, (v, f, ss) in Q.items() if ss == s]          # the fiscal year's Q1, if reported
        chain += lst
        chain.sort()
        for a, b in zip(chain, chain[1:]):
            if 75 <= _dur(a[0], b[0]) <= 105 and b[0] not in Q:
                Q[b[0]] = (b[1] - a[1], b[2], a[0])
    return sorted(((e, v, f) for e, (v, f, s) in Q.items()), key=lambda x: x[0])


def ttm_pair(quarters: list[tuple[str, float, str]], asof: str) -> Optional[tuple[float, float, str]]:
    """Point-in-time trailing-12-month EPS now and a year earlier, using only quarters first filed <= asof.

    Needs 8 consecutive quarters (ends 65..120 days apart) and a latest quarter within ~7 months of asof.
    Returns (ttm_now, ttm_prev, latest_quarter_end) or None.
    """
    q = [x for x in quarters if x[2] <= asof and x[0] <= asof]
    if len(q) < 8:
        return None
    last8 = q[-8:]
    for a, b in zip(last8, last8[1:]):
        if not (65 <= _dur(a[0], b[0]) <= 120):
            return None
    if _dur(last8[-1][0], asof) > 220:
        return None
    now = sum(x[1] for x in last8[4:])
    prev = sum(x[1] for x in last8[:4])
    return now, prev, last8[-1][0]


def report_due_dates(d202: Iterable[str], asof: str) -> set[str]:
    """Predicted next report dates: each past Item 2.02 date + 364 days."""
    out = set()
    for s in d202:
        try:
            if s <= asof:
                out.add((_d(s) + timedelta(days=364)).isoformat())
        except Exception:
            pass
    return out


def build_rows(cands: list[dict], sessions: list[str], formation: str) -> dict:
    """cands: [{t, l, b, dv20, ttm, ttm_prev, d202(list)}] that already passed the price/volume/split filters.

    Applies the 'matchable' definition, the Liquid median cut, c2 (top 20%) and c3, and returns
    {"rows": [...], "stats": {...}}. Rows keep the order of `cands` (that order is the tie-break in flags()).
    """
    target = session_offset(sessions, formation, REPORT_LEAD_SESSIONS)
    M = []
    for c in cands:
        if not (c.get("l", 0) >= MIN_PRICE and c.get("dv20", 0) >= MIN_DV20):
            continue
        if not (c.get("b", 0) > 0 and c.get("ttm") is not None and c.get("ttm_prev") is not None and c.get("d202")):
            continue
        M.append(c)
    stats = {"formation": formation, "report_target": target, "matchable": len(M)}
    if not M:
        stats.update(liquid=0, c2=0, c3=0)
        return {"rows": [], "stats": stats}
    dvs = sorted(c["dv20"] for c in M)
    n = len(dvs)
    median = dvs[n // 2] if n % 2 else 0.5 * (dvs[n // 2 - 1] + dvs[n // 2])
    score = {c["t"]: (c["ttm"] - c["ttm_prev"]) / c["l"] for c in M}
    top_n = max(1, math.ceil(C2_TOP_FRAC * len(M)))
    ranked = sorted(M, key=lambda c: (-score[c["t"]], c["t"]))
    c2set = {c["t"] for c in ranked[:top_n]}
    rows = []
    for c in M:
        if c["dv20"] < median:
            continue
        due = report_due_dates(c["d202"], formation)
        rows.append({"t": c["t"], "c2": 1 if c["t"] in c2set else 0,
                     "c3": 1 if (target and target in due) else 0, "b": c["b"], "l": c["l"]})
    stats.update(liquid=len(rows), c2=sum(r["c2"] for r in rows), c3=sum(r["c3"] for r in rows),
                 median_dv20=median, c2_cut_count=top_n)
    return {"rows": rows, "stats": stats}


# ───────────────────────────── state / files ─────────────────────────────
def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


_STATE_CACHE: dict[str, Any] = {"mtime": None, "path": None, "data": None}


def load_state() -> dict:
    p = STATE_PATH()
    try:
        m = p.stat().st_mtime_ns
    except Exception:
        m = None
    if _STATE_CACHE["path"] == str(p) and _STATE_CACHE["mtime"] == m and _STATE_CACHE["data"] is not None:
        return _STATE_CACHE["data"]
    st = _read_json(p, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("positions", {})
    st.setdefault("last_flag", {})
    st.setdefault("flagged_today", {"date": "", "syms": []})
    st.setdefault("entries_today", {"date": "", "n": 0})
    _STATE_CACHE.update(mtime=m, path=str(p), data=st)
    return st


def save_state(st: dict) -> None:
    p = STATE_PATH()
    _write_json(p, st)
    try:
        _STATE_CACHE.update(mtime=p.stat().st_mtime_ns, path=str(p), data=st)
    except Exception:
        _STATE_CACHE["data"] = None


def is_held(symbol: str) -> bool:
    """True if `symbol` is an open (or being-opened) scoutTiq position."""
    try:
        return str(symbol).upper() in load_state().get("positions", {})
    except Exception:
        return False


def append_flag_log(rec: dict) -> None:
    try:
        p = FLAGS_PATH()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        logger.exception("scouttiq flag log write failed")


# ───────────────────────────── order chokepoint guard ─────────────────────────────
_GUARD_LOG: dict[str, float] = {}


def _guard_log(key: str, msg: str) -> None:
    now = _time.time()
    if now - _GUARD_LOG.get(key, 0.0) >= 600:
        _GUARD_LOG[key] = now
        logger.warning(msg)


async def order_verdict(payload: dict, short_open: bool, long_qty_lookup) -> Optional[str]:
    """Called from execution._post_order. Returns a reason string if the order must be refused, else None.

      * scoutTiq's own orders always pass.
      * A SELL of a scoutTiq-held symbol is refused (no stops, no EOD flatten, no news exit: it is held 5 sessions).
      * With SCOUTTIQ_ONLY: any other BUY is refused unless it covers an existing short, and so is any new short.
    """
    if SCOUTTIQ_CTX.get():
        return None
    if not isinstance(payload, dict):
        return None
    side = payload.get("side")
    side = str(getattr(side, "value", side)).lower()
    symbol = str(payload.get("symbol", "")).upper()
    if side == "sell":
        if is_held(symbol):
            _guard_log("hold:" + symbol, "scouttiq_hold: refusing sell of %s (held until its 5-session exit)" % symbol)
            return "scouttiq_hold"
        if only_mode() and short_open:
            _guard_log("short:" + symbol, "scouttiq_only: refusing new short %s" % symbol)
            return "scouttiq_only_short"
        return None
    if side == "buy" and only_mode():
        try:
            q = await long_qty_lookup(symbol)
        except Exception:
            q = None
        if q is not None and q < 0:
            return None                                 # buy-to-cover an old short: allowed
        _guard_log("buy:" + symbol, "scouttiq_only: refusing non-scoutTiq buy of %s" % symbol)
        return "scouttiq_only_buy"
    return None


# ───────────────────────────── SEC access (nightly job) ─────────────────────────────
class SecClient:
    """Tiny polite SEC client. SEC_USER_AGENT must be set (SEC requires a contact string); never hard-coded here."""

    def __init__(self):
        import httpx
        ua = _env("SEC_USER_AGENT")
        if not ua:
            raise RuntimeError("SEC_USER_AGENT is not set (SEC requires 'Name contact@email' in the User-Agent)")
        self.c = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip"}, timeout=60, follow_redirects=True)
        self.gap = 1.0 / max(1.0, _env_f("SCOUTTIQ_SEC_RPS", 6.0))
        self.last = 0.0
        self.calls = 0

    def get(self, url: str):
        for attempt in range(5):
            wait = self.gap - (_time.time() - self.last)
            if wait > 0:
                _time.sleep(wait)
            self.last = _time.time()
            try:
                r = self.c.get(url)
            except Exception as exc:
                logger.warning("scouttiq SEC transport error %s (%s)", url, exc)
                _time.sleep(2 * (attempt + 1))
                continue
            self.calls += 1
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429, 500, 502, 503, 504):
                _time.sleep(3 * (attempt + 1))
                continue
            logger.warning("scouttiq SEC %s -> %s", url, r.status_code)
            return None
        return None


def _cik10(cik) -> str:
    return str(int(cik)).zfill(10)


def parse_submissions(js: dict) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """-> (item_2_02_dates, [(form, accession)] of 10-Q/10-K newest first, older_page_names)"""
    rec = (js.get("filings") or {}).get("recent") or js
    forms, dates, items, accs = rec.get("form") or [], rec.get("filingDate") or [], rec.get("items") or [], rec.get("accessionNumber") or []
    d202, periodic = [], []
    for i, f in enumerate(forms):
        it = items[i] if i < len(items) else ""
        if f in ("8-K", "8-K/A") and "2.02" in str(it):
            d202.append(dates[i])
        if f in ("10-Q", "10-K", "10-Q/A", "10-K/A") and i < len(accs):
            periodic.append((dates[i], accs[i]))
    older = [x.get("name") for x in ((js.get("filings") or {}).get("files") or []) if x.get("name")]
    return d202, periodic, older


EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")


def extract_eps(facts_js: dict) -> list[list]:
    ug = ((facts_js.get("facts") or {}).get("us-gaap") or {})
    for tag in EPS_TAGS:
        node = ug.get(tag)
        if not node:
            continue
        units = (node.get("units") or {})
        arr = units.get("USD/shares") or next(iter(units.values()), [])
        rows = [[x["start"], x["end"], x["val"], x["filed"], x.get("form", "")]
                for x in arr if x.get("start") and x.get("end") and x.get("val") is not None
                and str(x.get("form", "")).startswith(("10-Q", "10-K"))]
        if rows:
            return rows
    return []


def refresh_company(sec: SecClient, store: dict, cik: str, today: str, force_facts: bool = False) -> bool:
    """Refresh one company's record in `store` (mutated). Returns True if anything was fetched."""
    rec = store.get(cik) or {"d202": [], "per": "", "eps": [], "updated": ""}
    r = sec.get("https://data.sec.gov/submissions/CIK%s.json" % _cik10(cik))
    if r is None:
        return False
    js = r.json()
    d202, periodic, older = parse_submissions(js)
    oldest = min([x for x in (js.get("filings", {}).get("recent", {}).get("filingDate") or [])] or [today])
    if older and oldest > (_d(today) - timedelta(days=430)).isoformat():
        for name in older[:2]:
            rr = sec.get("https://data.sec.gov/submissions/" + name)
            if rr is not None:
                d2, p2, _ = parse_submissions(rr.json())
                d202 += d2
    rec["d202"] = sorted(set(d202) | set(rec.get("d202") or []))
    latest_per = periodic[0][1] if periodic else ""
    need_facts = force_facts or not rec.get("eps") or (latest_per and latest_per != rec.get("per"))
    if need_facts:
        rf = sec.get("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % _cik10(cik))
        if rf is not None:
            rec["eps"] = extract_eps(rf.json())
            rec["per"] = latest_per
    rec["updated"] = today
    store[cik] = rec
    return True


# ───────────────────────────── Alpaca data helpers (sync, used by the nightly job) ─────────────────────────────
def _alpaca_headers() -> dict:
    from backend import config
    return {"APCA-API-KEY-ID": config.ALPACA_API_KEY or "", "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY or ""}


def _data_url() -> str:
    from backend import config
    return str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")


def _trading_url() -> str:
    from backend import config
    return str(getattr(config, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")).rstrip("/")


def fetch_calendar_sync(start: str, end: str) -> list[str]:
    import httpx
    try:
        r = httpx.get(_trading_url() + "/v2/calendar", params={"start": start, "end": end}, headers=_alpaca_headers(), timeout=30)
        if r.status_code == 200:
            return sorted(x["date"] for x in r.json())
    except Exception:
        logger.exception("scouttiq calendar fetch failed")
    return []


def fetch_daily_bars_sync(symbols: list[str], start: str, end: str, feed: str) -> dict[str, list[tuple[str, float, float]]]:
    """-> {sym: [(date, close, volume)]}, raw (unadjusted) prices."""
    import httpx
    out: dict[str, list] = {}
    hdr = _alpaca_headers()
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
                    r = c.get(_data_url() + "/v2/stocks/bars", params=params, headers=hdr)
                    if r.status_code == 429:
                        _time.sleep(5 * (attempt + 1))
                        continue
                    break
                if r is None or r.status_code != 200:
                    raise RuntimeError("bars request failed: %s %s" % (getattr(r, "status_code", None), getattr(r, "text", "")[:200]))
                js = r.json()
                for sym, arr in (js.get("bars") or {}).items():
                    out.setdefault(sym, []).extend((b["t"][:10], float(b["c"]), float(b["v"])) for b in arr)
                token = js.get("next_page_token")
                if not token:
                    break
            _time.sleep(0.4)
    for s in out:
        out[s].sort()
    return out


def fetch_splits_sync(symbols: list[str], start: str, end: str) -> Optional[set[str]]:
    """Symbols with a forward/reverse split in [start, end]. None if the corporate-actions endpoint is unavailable."""
    import httpx
    hit: set[str] = set()
    hdr = _alpaca_headers()
    try:
        with httpx.Client(timeout=60) as c:
            token = None
            while True:
                params = {"types": "forward_split,reverse_split,unit_split,stock_dividend", "start": start, "end": end, "limit": 1000}
                if token:
                    params["page_token"] = token
                r = c.get(_data_url() + "/v1/corporate-actions", params=params, headers=hdr)
                if r.status_code != 200:
                    return None
                js = r.json()
                ca = js.get("corporate_actions") or {}
                for kind in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends"):
                    for x in ca.get(kind) or []:
                        if x.get("symbol"):
                            hit.add(x["symbol"])
                token = js.get("next_page_token")
                if not token:
                    break
        return hit
    except Exception:
        logger.exception("scouttiq corporate-actions fetch failed")
        return None


def split_heuristic(bars: list[tuple[str, float, float]], lookback: int = 21) -> bool:
    """Fallback when the corporate-actions feed is unavailable: a one-day raw-close jump beyond 40% down / 70% up."""
    cl = [b[1] for b in bars[-(lookback + 1):]]
    for a, b in zip(cl, cl[1:]):
        if a > 0 and (b / a <= 0.6 or b / a >= 1.7):
            return True
    return False


# ───────────────────────────── nightly job ─────────────────────────────
def build_base(force: bool = False, sec_budget_calls: Optional[int] = None) -> dict:
    """Build tonight's base from scratch (blocking; run it in a thread). Writes scouttiq_base.json.

    Steps: universe -> daily bars (raw, SIP) -> price/$volume/split filter -> SEC refresh -> c2/c3 -> write.
    `sec_budget_calls` caps SEC requests per run (the first full seed takes several runs on a small budget;
    the CLI `python -m backend.scouttiq build` runs it uncapped).
    """
    t0 = _time.time()
    from backend import config
    today_et = datetime.now(ET).date()
    cal = fetch_calendar_sync((today_et - timedelta(days=60)).isoformat(), (today_et + timedelta(days=60)).isoformat())
    if not cal:
        cal = weekday_sessions(today_et - timedelta(days=60), today_et + timedelta(days=60))
        logger.warning("scouttiq: Alpaca calendar unavailable, using weekdays (holidays ignored)")
    past = [s for s in cal if s <= today_et.isoformat()]
    feed = _env("SCOUTTIQ_BARS_FEED", "sip")
    try:
        allsyms = list(config.load_tradable_equities())          # the whole tradable universe, not the bot's scan list
    except Exception:
        allsyms = list(getattr(config, "SYMBOLS", None) or [])
    syms = [s for s in allsyms if s.isalpha() and len(s) <= 5]
    logger.info("scouttiq build: %d tradable symbols in the universe", len(syms))
    end_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (today_et - timedelta(days=50)).isoformat()
    bars = fetch_daily_bars_sync(sorted(set(syms) | {"SPY"}), start, end_ts, feed)
    _n_et = datetime.now(ET)
    if _n_et.date().isoformat() in cal and (_n_et.hour, _n_et.minute) < (16, 20):
        # today's daily bar is still forming (or not yet final): the base must come from completed sessions only
        for _s in list(bars):
            bars[_s] = [b for b in bars[_s] if b[0] < _n_et.date().isoformat()]
        logger.info("scouttiq build: before 16:20 ET, ignoring today's unfinished bar")
    if "SPY" not in bars or not bars["SPY"]:
        raise RuntimeError("no SPY bars: cannot fix the formation date")
    formation = bars["SPY"][-1][0]
    logger.info("scouttiq build: formation date %s, %d symbols with bars, feed=%s", formation, len(bars), feed)
    cands, no_split_api = [], False
    splits = fetch_splits_sync(list(bars.keys()), (_d(formation) - timedelta(days=30)).isoformat(), formation)
    if splits is None:
        no_split_api = True
        logger.warning("scouttiq: corporate-actions feed unavailable, using the 40%%/70%% one-day-jump heuristic")
    for s, bs in bars.items():
        if s == "SPY" and "SPY" not in syms:
            continue
        bs = [b for b in bs if b[0] <= formation]
        if len(bs) < 20 or bs[-1][0] != formation:
            continue
        l = bs[-1][1]
        if l < MIN_PRICE:
            continue
        dv = sum(c * v for _, c, v in bs[-20:]) / 20.0
        if dv < MIN_DV20:
            continue
        if (splits is not None and s in splits) or (no_split_api and split_heuristic(bs)):
            continue
        cands.append({"t": s, "l": l, "b": bs[-5][1], "dv20": dv})
    cands.sort(key=lambda c: c["t"])                    # base-file order = alphabetical (tie-break)
    logger.info("scouttiq build: %d names pass price/$volume/split", len(cands))

    # SEC
    sec = SecClient()
    r = sec.get("https://www.sec.gov/files/company_tickers.json")
    if r is None:
        raise RuntimeError("could not load the SEC ticker map")
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in r.json().values()}
    store = _read_json(SEC_PATH(), {})
    if not isinstance(store, dict):
        store = {}
    todo = []
    for c in cands:
        cik = t2c.get(c["t"])
        c["cik"] = cik
        if not cik:
            continue
        rec = store.get(cik)
        if rec is None:
            todo.append((0, cik))                       # never fetched first
        elif rec.get("updated", "") < formation:
            todo.append((1, cik))
    todo.sort()
    done = 0
    for _, cik in todo:
        if sec_budget_calls is not None and sec.calls >= sec_budget_calls:
            break
        try:
            refresh_company(sec, store, cik, today_et.isoformat())
        except Exception:
            logger.exception("scouttiq SEC refresh failed for CIK %s", cik)
        done += 1
        if done % 100 == 0:
            _write_json(SEC_PATH(), store)
            logger.info("scouttiq build: SEC refreshed %d/%d (calls=%d)", done, len(todo), sec.calls)
    _write_json(SEC_PATH(), store)

    # scores
    for c in cands:
        rec = store.get(c.get("cik") or "")
        c["ttm"] = c["ttm_prev"] = None
        c["d202"] = (rec or {}).get("d202") or []
        if rec and rec.get("eps"):
            pair = ttm_pair(quarterly_eps(rec["eps"]), formation)
            if pair:
                c["ttm"], c["ttm_prev"] = pair[0], pair[1]
    built = build_rows(cands, cal, formation)
    st = built["stats"]
    st.update(candidates=len(cands), sec_pending=sum(1 for c in cands if c.get("cik") and c["ttm"] is None),
              no_cik=sum(1 for c in cands if not c.get("cik")), feed=feed, split_source="heuristic" if no_split_api else "alpaca",
              sec_calls=sec.calls, seconds=round(_time.time() - t0, 1), built_at=datetime.now(timezone.utc).isoformat())
    out = {"formation_date": formation, "stats": st, "rows": built["rows"]}
    _write_json(data_dir() / "scouttiq_base.json", out)
    try:
        _write_json(data_dir() / ("scouttiq_base_%s.json" % formation), out)
    except Exception:
        pass
    logger.info("scouttiq build DONE: %s", st)
    return out


def load_base() -> Optional[dict]:
    b = _read_json(BASE_PATH(), None)
    if not isinstance(b, dict) or not b.get("rows") or not b.get("formation_date"):
        return None
    return b


# ───────────────────────────── live prices (async) ─────────────────────────────
async def fetch_prices(symbols: list[str], today: str, feed: str) -> dict[str, tuple[float, str]]:
    """Latest trade per symbol -> {sym: (price, trade_date_et)}. Only names that traded are returned."""
    import httpx
    from backend import config
    hdr = {"APCA-API-KEY-ID": config.ALPACA_API_KEY or "", "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY or ""}
    url = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/") + "/v2/stocks/trades/latest"
    out: dict[str, tuple[float, str]] = {}
    async with httpx.AsyncClient(timeout=30) as c:
        for i in range(0, len(symbols), 200):
            chunk = symbols[i:i + 200]
            for attempt in range(4):
                try:
                    r = await c.get(url, params={"symbols": ",".join(chunk), "feed": feed}, headers=hdr)
                except Exception as exc:
                    logger.warning("scouttiq price fetch error: %s", exc)
                    await asyncio.sleep(2)
                    continue
                if r.status_code == 429:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning("scouttiq price fetch %s %s", r.status_code, r.text[:160])
                    break
                for sym, tr in (r.json().get("trades") or {}).items():
                    try:
                        ts = datetime.fromisoformat(tr["t"].replace("Z", "+00:00")).astimezone(ET)
                        out[sym] = (float(tr["p"]), ts.date().isoformat())
                    except Exception:
                        pass
                break
    return out


def valid_prices(raw: dict[str, tuple[float, str]], today: str) -> dict[str, float]:
    """'Stamped today' filter; positivity and the 45% band are enforced inside flags()."""
    return {s: p for s, (p, d) in raw.items() if d == today and p > 0}


# ───────────────────────────── agent: entries, exits, loop ─────────────────────────────
_RUN: dict[str, Any] = {"cutoff": None, "cutoff_ts": 0.0, "last_a": 0.0, "last_b": 0.0, "sessions": None, "sessions_day": "",
                        "build_day": "", "build_running": False, "build_fail_ts": 0.0}


async def _sessions(today: str) -> list[str]:
    if _RUN["sessions"] and _RUN["sessions_day"] == today:
        return _RUN["sessions"]
    d = _d(today)
    cal = await asyncio.to_thread(fetch_calendar_sync, (d - timedelta(days=15)).isoformat(), (d + timedelta(days=45)).isoformat())
    if not cal:
        cal = weekday_sessions(d - timedelta(days=15), d + timedelta(days=45))
    _RUN["sessions"], _RUN["sessions_day"] = cal, today
    return cal


def _roll_counters(st: dict, today: str) -> None:
    if st["flagged_today"].get("date") != today:
        st["flagged_today"] = {"date": today, "syms": []}
    if st["entries_today"].get("date") != today:
        st["entries_today"] = {"date": today, "n": 0}


async def _enter(sym: str, flag_price: float, version: str, info: dict, st: dict, today: str, sessions: list[str]) -> None:
    """Place the buy for one new flag (mode 'on') and register the position."""
    from backend.execution import place_market_buy, get_latest_price
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "date": today, "symbol": sym, "version": version,
           "flag_price": round(flag_price, 4), "mode": mode(), **info, "taken": False, "why_not": None}
    max_pos = _env_i("SCOUTTIQ_MAX_POSITIONS", 40)
    max_day = _env_i("SCOUTTIQ_MAX_NEW_PER_DAY", 10)
    usd = _env_f("SCOUTTIQ_POSITION_USD", 500.0)
    if mode() != "on":
        rec["why_not"] = "shadow"
    elif sym in st["positions"]:
        rec["why_not"] = "already_held"
    elif len(st["positions"]) >= max_pos:
        rec["why_not"] = "max_positions"
    elif st["entries_today"]["n"] >= max_day:
        rec["why_not"] = "max_new_per_day"
    else:
        px = await get_latest_price(sym) or flag_price
        qty = int(usd // px) if px else 0
        if qty < 1:
            rec["why_not"] = "qty_lt_1"
        else:
            exit_date = session_offset(sessions, today, HOLD_SESSIONS)
            # register BEFORE the order so every other exit loop already treats the symbol as held
            st["positions"][sym] = {"entry_date": today, "exit_date": exit_date, "qty": qty, "flag_price": flag_price,
                                    "version": version, "state": "pending", "ts": rec["ts"]}
            save_state(st)
            tok = SCOUTTIQ_CTX.set(True)
            try:
                order_id, fill = await place_market_buy(sym, qty)
            finally:
                SCOUTTIQ_CTX.reset(tok)
            if not order_id:
                st["positions"].pop(sym, None)
                save_state(st)
                rec["why_not"] = "order_failed"
            else:
                entry_px = fill or px
                st["positions"][sym].update(state="open", order_id=order_id, entry_price=entry_px)
                st["entries_today"]["n"] += 1
                rec.update(taken=True, order_id=order_id, entry_price=entry_px, qty=qty, exit_date=exit_date)
                try:
                    from backend.services.trade_ledger_service import load_ledger, add_entry
                    from backend.botv3 import save_ledger
                    ledger = load_ledger()
                    add_entry(ledger, symbol=sym, side="long", qty=qty, entry_price=entry_px, order_id=order_id, strategy="scouttiq",
                              cooldown_minutes=0,
                              metadata={"source": "scouttiq", "version": version, "flag_price": flag_price,
                                        "exit_date": exit_date, "hold_sessions": HOLD_SESSIONS, **info})
                    save_ledger(ledger)
                except Exception:
                    logger.exception("scouttiq ledger entry failed for %s", sym)
                logger.info("SCOUTTIQ ENTRY %s qty=%d @%.2f version=%s exit=%s info=%s", sym, qty, entry_px, version, exit_date, info)
    st["last_flag"][sym] = today
    save_state(st)
    append_flag_log(rec)


def _repeat_blocked(st: dict, sym: str, today: str, sessions: list[str]) -> bool:
    last = st["last_flag"].get(sym)
    if not last:
        return False
    nxt = session_offset(sessions, last, HOLD_SESSIONS)
    return bool(nxt and today <= nxt)


async def _live_cycle(base: dict, today: str, now_et: datetime, sessions: list[str], full: bool) -> None:
    st = load_state()
    _roll_counters(st, today)
    rows = base["rows"]
    age = (_d(today) - _d(base["formation_date"])).days
    if age > MAX_BASE_AGE_DAYS:
        _guard_log("stale_base", "scouttiq: base is %d days old (> %d): skipping cycles" % (age, MAX_BASE_AGE_DAYS))
        return
    feed = _env("SCOUTTIQ_FEED", "iex")
    if full:
        raw = await fetch_prices([r["t"] for r in rows], today, feed)
        prices = valid_prices(raw, today)
        cyc = full_cycle(rows, prices)
        logger.info("SCOUTTIQ A cycle: valid %d/%d skipped=%s cutoff=%s flags=%d", cyc["n_valid"], cyc["n_total"],
                    cyc["skipped"], None if cyc["cutoff"] is None else round(cyc["cutoff"], 4), len(cyc["flags"]))
        if cyc["skipped"]:
            return
        _RUN["cutoff"], _RUN["cutoff_ts"] = cyc["cutoff"], _time.time()
        picks = [(t, "A", prices[t], {"r5": round(cyc["r5"][t], 5)}) for t in cyc["flags"]]
    else:
        if _RUN["cutoff"] is None or _time.time() - _RUN["cutoff_ts"] > 20 * 60:
            return
        watch = [r for r in rows if r["c2"] or r["c3"]]
        raw = await fetch_prices([r["t"] for r in watch], today, feed)
        prices = valid_prices(raw, today)
        picks = [(t, "B", trig, {"r5": round(r5, 5)}) for t, r5, trig in version_b(watch, prices, _RUN["cutoff"])]
    byt = {r["t"]: r for r in rows}
    for t, ver, px, info in picks:
        if t in st["flagged_today"]["syms"]:
            continue                                                   # first flag per stock per session, never moved
        st["flagged_today"]["syms"].append(t)
        if _repeat_blocked(st, t, today, sessions):
            append_flag_log({"ts": datetime.now(timezone.utc).isoformat(), "date": today, "symbol": t, "version": ver,
                             "flag_price": round(px, 4), "taken": False, "why_not": "repeat_within_5_sessions", **info})
            continue
        r = byt[t]
        info = {**info, "c2": r["c2"], "c3": r["c3"], "b": r["b"], "l": r["l"], "base_date": base["formation_date"]}
        await _enter(t, px, ver, info, st, today, sessions)


async def _exit_pass(today: str, now_et: datetime, sessions: list[str]) -> None:
    """Sell scoutTiq positions that are due (exit date reached) once it is 15:56 ET or later; overdue ones any time."""
    st = load_state()
    if not st["positions"]:
        return
    from backend.execution import place_market_sell, _get_open_positions
    stop_pct = _env_f("SCOUTTIQ_HARD_STOP_PCT", 0.0)               # optional safety net; 0 = off (the scored rule has none)
    hm = (now_et.hour, now_et.minute)
    due = any(p.get("exit_date") and (today > p["exit_date"] or (today == p["exit_date"] and hm >= (15, 56)))
              for p in st["positions"].values())
    pending = any(p.get("state") == "pending" for p in st["positions"].values())
    if not (due or pending or stop_pct > 0) and _time.time() - _RUN.get("last_live_check", 0.0) < 900:
        return                                                      # nothing due: no broker call
    _RUN["last_live_check"] = _time.time()
    live = {}
    try:
        for p in await _get_open_positions():
            live[str(p.get("symbol", "")).upper()] = p
    except Exception:
        live = None
    changed = False
    for sym, pos in list(st["positions"].items()):
        if pos.get("state") != "open":
            if pos.get("state") == "pending":
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["ts"])).total_seconds()
                if age > 900 and live is not None and sym not in live:
                    st["positions"].pop(sym, None)
                    changed = True
            continue
        if live is not None and sym not in live:
            logger.info("SCOUTTIQ position %s no longer at broker: dropping from state", sym)
            st["positions"].pop(sym, None)
            changed = True
            continue
        reason = None
        ex = pos.get("exit_date") or ""
        if ex and today > ex:
            reason = "scouttiq_5d_exit_overdue"
        elif ex and today == ex and (now_et.hour, now_et.minute) >= (15, 56):
            reason = "scouttiq_5d_exit"
        if reason is None and stop_pct > 0 and live is not None:
            try:
                if float(live[sym].get("unrealized_plpc") or 0) <= -abs(stop_pct) / 100.0:
                    reason = "scouttiq_hard_stop"
            except Exception:
                pass
        if reason is None:
            continue
        if not (9, 31) <= (now_et.hour, now_et.minute) < (16, 0):
            continue
        tok = SCOUTTIQ_CTX.set(True)
        try:
            qty = float(live[sym].get("qty")) if live and sym in live else float(pos.get("qty") or 0)
            order_id = await place_market_sell(sym, qty)
        finally:
            SCOUTTIQ_CTX.reset(tok)
        logger.info("SCOUTTIQ EXIT %s reason=%s order=%s", sym, reason, order_id)
        if order_id:
            try:
                from backend.services.trade_ledger_service import load_ledger
                from backend.botv3 import close_entry, save_ledger
                ledger = load_ledger()
                close_entry(ledger, symbol=sym, order_id=order_id, exit_price=None, reason=reason, cooldown_minutes=0)
                save_ledger(ledger)
            except Exception:
                logger.exception("scouttiq ledger close failed for %s", sym)
            st["positions"].pop(sym, None)
            changed = True
            append_flag_log({"ts": datetime.now(timezone.utc).isoformat(), "date": today, "symbol": sym, "event": "exit",
                             "reason": reason, "order_id": order_id})
    if changed:
        save_state(st)


async def _maybe_build(today: str, now_et: datetime) -> None:
    """After the close, once per session day, rebuild the base in a worker thread."""
    if _RUN["build_running"] or _RUN["build_day"] == today:
        return
    if (now_et.hour, now_et.minute) < (16, 25):
        return
    b = load_base()
    if b and b.get("formation_date") == today and not _env("SCOUTTIQ_BASE_PATH"):
        _RUN["build_day"] = today
        return
    if _env("SCOUTTIQ_BASE_PATH"):                       # base comes from an external file: never build our own
        _RUN["build_day"] = today
        return
    if _time.time() - _RUN["build_fail_ts"] < 1800:
        return
    _RUN["build_running"] = True
    try:
        budget = _env_i("SCOUTTIQ_SEC_BUDGET_CALLS", 6000)
        logger.info("SCOUTTIQ nightly build starting (sec budget %d calls)", budget)
        await asyncio.to_thread(build_base, False, budget)
        _RUN["build_day"] = today
    except Exception:
        logger.exception("scouttiq nightly build failed (will retry in 30 min)")
        _RUN["build_fail_ts"] = _time.time()
    finally:
        _RUN["build_running"] = False


async def monitor_scouttiq() -> None:
    """Background task started from main.py. Does nothing at all when SCOUTTIQ_MODE is off."""
    if mode() == "off":
        logger.info("scouttiq: mode off (set SCOUTTIQ_MODE=shadow or on)")
        return
    logger.info("scouttiq: started mode=%s only=%s", mode(), only_mode())
    cycle_a = max(5, _env_i("SCOUTTIQ_CYCLE_MINUTES", 15)) * 60
    cycle_b = 5 * 60
    while True:
        try:
            now_et = datetime.now(ET)
            today = now_et.date().isoformat()
            if now_et.weekday() < 5:
                sessions = await _sessions(today)
                if today in sessions:
                    hm = (now_et.hour, now_et.minute)
                    if (9, 35) <= hm < (16, 0):
                        base = load_base()
                        if base is None:
                            _guard_log("nobase", "scouttiq: no base file yet (run `python -m backend.scouttiq build`)")
                        else:
                            t = _time.time()
                            if t - _RUN["last_a"] >= cycle_a:
                                _RUN["last_a"] = _RUN["last_b"] = t
                                await _live_cycle(base, today, now_et, sessions, full=True)
                            elif t - _RUN["last_b"] >= cycle_b:
                                _RUN["last_b"] = t
                                await _live_cycle(base, today, now_et, sessions, full=False)
                    if mode() == "on":
                        await _exit_pass(today, now_et, sessions)
                    await _maybe_build(today, now_et)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scouttiq loop error")
        await asyncio.sleep(30)


# ───────────────────────────── CLI ─────────────────────────────
def _selftest() -> int:
    from backend.scouttiq_selftest import run
    return run()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build":
        r = build_base(force=True, sec_budget_calls=None)
        print(json.dumps(r["stats"], indent=1))
    elif cmd == "status":
        b = load_base()
        st = load_state()
        print("mode=%s only=%s" % (mode(), only_mode()))
        print("base:", None if not b else {"formation": b["formation_date"], "rows": len(b["rows"]), **b.get("stats", {})})
        print("open scouttiq positions:", {k: (v.get("state"), v.get("entry_date"), v.get("exit_date")) for k, v in st["positions"].items()})
    elif cmd == "flags-now":
        b = load_base()
        today = datetime.now(ET).date().isoformat()
        raw = asyncio.run(fetch_prices([r["t"] for r in b["rows"]], today, _env("SCOUTTIQ_FEED", "iex")))
        cyc = full_cycle(b["rows"], valid_prices(raw, today))
        print({k: (v if k != "r5" else len(v)) for k, v in cyc.items()})
    elif cmd == "selftest":
        sys.exit(_selftest())
    else:
        print("usage: python -m backend.scouttiq [build|status|flags-now|selftest]")
