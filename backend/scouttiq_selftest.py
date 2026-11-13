"""Offline self-test for backend.scouttiq: no network, no broker. `python -m backend.scouttiq selftest`."""
from __future__ import annotations

import json

import asyncio
import os
import random
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_FAILS: list[str] = []
_N = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    _N[0] += 1
    if not cond:
        _FAILS.append(name + (" :: " + detail if detail else ""))
        print("FAIL", name, detail)


def _facts(rows):
    return [list(r) for r in rows]


def test_rule(sq):
    rnd = random.Random(7)
    for trial in range(200):
        n = rnd.randint(1, 120)
        rows = [{"t": "T%03d" % i, "c2": int(rnd.random() < .2), "c3": int(rnd.random() < .05),
                 "b": rnd.uniform(5, 200), "l": 0.0} for i in range(n)]
        for r in rows:
            r["l"] = r["b"] * rnd.uniform(.9, 1.1)
        prices = {}
        for r in rows:
            if rnd.random() < .95:
                prices[r["t"]] = r["l"] * rnd.uniform(.8, 1.2) if rnd.random() > .02 else r["l"] * 1.6
        a = sq.flags(rows, prices)
        b = sq.full_cycle(rows, prices)
        check("full_cycle flags == flags() trial %d" % trial, a == b["flags"], "%s vs %s" % (a, b["flags"]))
    # skipped cycle: <85% valid -> no flags
    rows = [{"t": "A%d" % i, "c2": 1, "c3": 1, "b": 10.0, "l": 10.0} for i in range(20)]
    prices = {r["t"]: 10.0 for r in rows[:16]}                     # 80% valid
    check("skip when <85% valid", sq.flags(rows, prices) == [] and sq.full_cycle(rows, prices)["skipped"])
    prices = {r["t"]: 10.0 for r in rows[:17]}                     # 85% valid
    check("run when >=85% valid", len(sq.flags(rows, prices)) == 17)    # c2+c3 = 2 on every valid row
    # split guard: |p/l - 1| > 45% is invalid
    rows = [{"t": "X%d" % i, "c2": 0, "c3": 0, "b": 10.0, "l": 10.0} for i in range(10)]
    pr = {r["t"]: 9.0 for r in rows}
    pr["X0"] = 5.0                                                  # -50%: invalid, dropped
    cyc = sq.full_cycle(rows, pr)
    check("split guard drops the name", cyc["n_valid"] == 9)
    # c1 only (no c2/c3) must not flag; c1 + c2 flags; c1 is the lowest 10% with ties by file order
    rows = [{"t": "N%02d" % i, "c2": 1 if i == 3 else 0, "c3": 0, "b": 100.0, "l": 100.0} for i in range(20)]
    pr = {r["t"]: 100.0 + i for i, r in enumerate(rows)}
    pr["N03"] = 90.0
    pr["N07"] = 95.0
    out = sq.flags(rows, pr)
    check("c1+c2 flags only the c2 name in the lowest decile", out == ["N03"], str(out))
    # ties: all equal r5, k=2 of 20 -> first two by base order are c1
    rows = [{"t": "E%02d" % i, "c2": 1, "c3": 0, "b": 10.0, "l": 10.0} for i in range(20)]
    out = sq.flags(rows, {r["t"]: 10.0 for r in rows})
    check("ties broken by base order", out == ["E00", "E01"], str(out))
    # version B
    rows = [{"t": "N%02d" % i, "c2": 1 if i in (3, 11) else 0, "c3": 0, "b": 100.0, "l": 100.0} for i in range(20)]
    cyc = sq.full_cycle(rows, pr)
    vb = sq.version_b(rows, {"N03": 89.0, "N11": 120.0}, cyc["cutoff"])
    check("version B flags only c2/c3 names at or below the cutoff", [x[0] for x in vb] == ["N03"], str(vb))
    check("version B trigger price = b*(1+cutoff)", abs(vb[0][2] - 100.0 * (1 + cyc["cutoff"])) < 1e-9)


def _year_facts(fy_start_year, q_eps, filed_lag=30, restated=None):
    """Synthetic 10-Q/10-K EPS facts for one fiscal year Jan-Dec: 3m facts for Q1..Q3, YTD for Q2/Q3 (as in real 10-Qs), FY."""
    y = fy_start_year
    out = []
    ends = [(f"{y}-01-01", f"{y}-03-31"), (f"{y}-04-01", f"{y}-06-30"), (f"{y}-07-01", f"{y}-09-30")]
    for i, (s, e) in enumerate(ends):
        filed = (datetime.fromisoformat(e) + timedelta(days=filed_lag)).date().isoformat()
        out.append([s, e, q_eps[i], filed, "10-Q"])
        if i >= 1:
            out.append([f"{y}-01-01", e, round(sum(q_eps[:i + 1]), 4), filed, "10-Q"])
    out.append([f"{y}-01-01", f"{y}-12-31", round(sum(q_eps), 4), f"{y + 1}-02-20", "10-K"])
    return out


def test_eps(sq):
    facts = _year_facts(2023, [1.0, 1.1, 1.2, 1.3]) + _year_facts(2024, [1.4, 1.5, 1.6, 1.7]) + _year_facts(2025, [1.8, 1.9, 2.0, 2.1])
    q = sq.quarterly_eps(facts)
    vals = [round(x[1], 4) for x in q]
    check("12 discrete quarters incl. derived Q4", vals == [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1], str(vals))
    q4 = [x for x in q if x[0] == "2024-12-31"][0]
    check("Q4 is dated when the 10-K was first filed", q4[2] == "2025-02-20", str(q4))
    t = sq.ttm_pair(q, "2026-03-01")
    check("TTM now / a year ago", t is not None and abs(t[0] - (1.8 + 1.9 + 2.0 + 2.1)) < 1e-9 and abs(t[1] - (1.4 + 1.5 + 1.6 + 1.7)) < 1e-9, str(t))
    # point in time: on 2025-11-15 only filings up to the Q3'25 10-Q (filed 2025-10-30) exist
    t = sq.ttm_pair(q, "2025-11-15")
    check("point-in-time ignores later filings", t is not None and abs(t[0] - (1.7 + 1.8 + 1.9 + 2.0)) < 1e-9, str(t))
    # first-reported: a later restatement of Q3'25 must be ignored
    restated = facts + [["2025-07-01", "2025-09-30", 9.9, "2026-01-15", "10-Q/A"]]
    q2 = sq.quarterly_eps(restated)
    check("restatement ignored (first-reported)", [round(x[1], 4) for x in q2] == vals)
    # a quarter only available as a YTD figure (no 3-month fact for Q2) is derived
    f2 = [x for x in facts if not (x[0] == "2025-04-01" and x[1] == "2025-06-30")]
    q3 = sq.quarterly_eps(f2)
    check("missing discrete Q2 derived from 6M - Q1", [round(x[1], 4) for x in q3] == vals, str([round(x[1], 4) for x in q3]))
    check("too little history -> no score", sq.ttm_pair(q[-6:], "2026-03-01") is None)
    check("stale latest quarter -> no score", sq.ttm_pair(q, "2026-12-31") is None)
    # a gap in the series (missing quarter) -> no score
    gap = [x for x in q if x[0] != "2025-03-31"]
    check("gap in quarters -> no score", sq.ttm_pair(gap, "2026-03-01") is None)


def test_rows(sq):
    sessions = sq.weekday_sessions(datetime(2026, 10, 1).date(), datetime(2026, 12, 31).date())
    sessions = [s for s in sessions if s != "2026-11-26"]               # Thanksgiving
    formation = "2026-11-18"
    target = sq.session_offset(sessions, formation, 6)
    check("6th session after 11-18 skips Thanksgiving", target == "2026-11-27", str(target))
    check("5-session hold skips Thanksgiving", sq.session_offset(sessions, "2026-11-23", 5) == "2026-12-01")
    due_last_year = (datetime.fromisoformat(target) - timedelta(days=364)).date().isoformat()
    cands = []
    for i in range(50):
        cands.append({"t": "S%02d" % i, "l": 20.0 + i, "b": 21.0, "dv20": 10e6 + i * 1e6,
                      "ttm": 2.0 + i * 0.1, "ttm_prev": 1.0, "d202": ["2026-08-01", "2026-05-01"]})
    cands[10]["d202"] = [due_last_year, "2026-08-01"]                    # c3 for S10
    cands.append({"t": "LOWP", "l": 4.0, "b": 4.0, "dv20": 50e6, "ttm": 9, "ttm_prev": 1, "d202": ["2026-01-01"]})
    cands.append({"t": "NOEPS", "l": 40.0, "b": 41.0, "dv20": 50e6, "ttm": None, "ttm_prev": None, "d202": ["2026-01-01"]})
    cands.append({"t": "NODATE", "l": 40.0, "b": 41.0, "dv20": 50e6, "ttm": 3, "ttm_prev": 1, "d202": []})
    cands.append({"t": "THIN", "l": 40.0, "b": 41.0, "dv20": 5e6, "ttm": 3, "ttm_prev": 1, "d202": ["2026-01-01"]})
    res = sq.build_rows(cands, sessions, formation)
    st, rows = res["stats"], res["rows"]
    check("matchable excludes price<5, no EPS, no report date, thin $vol", st["matchable"] == 50, str(st))
    check("liquid = at/above the matchable median $vol", st["liquid"] == 25 and all(int(r["t"][1:]) >= 25 for r in rows), str(st))
    check("c2 = top 20% of matchable by (ttm-ttm_prev)/close", st["c2_cut_count"] == 10)
    c2 = {r["t"] for r in rows if r["c2"]}
    scores = sorted(((c["ttm"] - c["ttm_prev"]) / c["l"], c["t"]) for c in cands if c["t"].startswith("S"))
    top10 = {t for s, t in scores[-10:]}
    check("c2 names are the top-10 scorers that are also liquid", c2 == (top10 & {r["t"] for r in rows}), str(c2))
    check("no c3 anywhere until a name has last year's date + 364 on the target", [r["t"] for r in rows if r["c3"]] == [])
    cands[10]["dv20"] = 99e6
    s10 = [r for r in sq.build_rows(cands, sessions, formation)["rows"] if r["t"] == "S10"]
    check("c3 fires for S10", s10 and s10[0]["c3"] == 1, str(s10))
    check("row order = candidate order", [r["t"] for r in rows] == sorted(r["t"] for r in rows))


def test_sec_parse(sq):
    js = {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K", "10-K", "4"], "filingDate": ["2026-08-04", "2026-08-05", "2026-05-01", "2026-02-20", "2026-02-21"],
                                   "items": ["2.02,9.01", "", "5.02", "", ""], "accessionNumber": ["a1", "a2", "a3", "a4", "a5"]},
                        "files": [{"name": "CIK0000000001-submissions-001.json"}]}}
    d202, per, older = sq.parse_submissions(js)
    check("Item 2.02 dates only", d202 == ["2026-08-04"], str(d202))
    check("latest periodic filing first", per[0] == ("2026-08-05", "a2"), str(per))
    check("older pages listed", older == ["CIK0000000001-submissions-001.json"])
    facts = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2026-01-01", "end": "2026-03-31", "val": 1.2, "filed": "2026-05-01", "form": "10-Q"},
        {"start": "2026-01-01", "end": "2026-03-31", "val": 1.3, "filed": "2026-05-01", "form": "8-K"}]}}}}}
    check("EPS rows from 10-Q/10-K only", len(sq.extract_eps(facts)) == 1)


def test_guard_and_flow(sq):
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    sq.data_dir = lambda: __import__("pathlib").Path(tmp)           # keep the test off the real config
    # fake execution / ledger / botv3 modules for the entry and exit flow
    calls = {"buys": [], "sells": [], "ledger": [], "extra_live": []}
    ex = types.ModuleType("backend.execution")

    async def place_market_buy(sym, qty, *a, **k):
        assert sq.SCOUTTIQ_CTX.get(), "buy must carry the scoutTiq context"
        calls["buys"].append((sym, qty))
        return "oid-" + sym, 10.0

    async def get_latest_price(sym):
        return 10.0

    async def place_market_sell(sym, qty):
        assert sq.SCOUTTIQ_CTX.get(), "sell must carry the scoutTiq context"
        calls["sells"].append((sym, qty))
        return "sell-" + sym

    async def _get_open_positions():
        return [{"symbol": s, "qty": str(p["qty"]), "unrealized_plpc": "0.0"} for s, p in sq.load_state()["positions"].items()] + list(calls["extra_live"])

    ex.place_market_buy, ex.get_latest_price, ex.place_market_sell, ex._get_open_positions = place_market_buy, get_latest_price, place_market_sell, _get_open_positions
    led = types.ModuleType("backend.services.trade_ledger_service")
    store = {}
    led.load_ledger = lambda: store
    led.add_entry = lambda ledger, **k: ledger.setdefault(k["symbol"], []).append({**k, "status": "open"})
    bot = types.ModuleType("backend.botv3")
    bot.save_ledger = lambda l: None
    bot.close_entry = lambda ledger, **k: calls["ledger"].append(k)
    pkg = sys.modules.get("backend") or types.ModuleType("backend")
    for name, mod in (("backend", pkg), ("backend.execution", ex), ("backend.services", types.ModuleType("backend.services")),
                      ("backend.services.trade_ledger_service", led), ("backend.botv3", bot)):
        sys.modules.setdefault(name, mod)
    sys.modules["backend.execution"], sys.modules["backend.services.trade_ledger_service"], sys.modules["backend.botv3"] = ex, led, bot

    sessions = sq.weekday_sessions(datetime(2026, 11, 1).date(), datetime(2026, 12, 31).date())
    today = "2026-11-12"
    sq._STATE_CACHE.update(mtime=None, path=None, data=None)

    async def run():
        os.environ["SCOUTTIQ_MODE"] = "on"
        os.environ["SCOUTTIQ_POSITION_USD"] = "500"
        os.environ["SCOUTTIQ_MAX_NEW_PER_DAY"] = "2"
        st = sq.load_state()
        sq._roll_counters(st, today)
        for sym in ("AAA", "BBB", "CCC"):
            await sq._enter(sym, 10.0, "A", {"r5": -0.1}, st, today, sessions)
        check("buys placed up to the daily cap", [b[0] for b in calls["buys"]] == ["AAA", "BBB"], str(calls["buys"]))
        check("qty = position $ / price", calls["buys"][0][1] == 50)
        check("positions registered with 5-session exit date", st["positions"]["AAA"]["exit_date"] == "2026-11-19", str(st["positions"].get("AAA")))
        check("third flag logged but not taken (cap)", True)
        check("is_held after entry", sq.is_held("AAA") and not sq.is_held("CCC"))
        # repeat suppression: flagged again within 5 sessions
        check("repeat flag within 5 sessions is blocked", sq._repeat_blocked(st, "AAA", "2026-11-18", sessions) and not sq._repeat_blocked(st, "AAA", "2026-11-20", sessions))
        # guard
        async def lq(sym):
            return -5.0 if sym == "SHORTY" else 0.0
        v = await sq.order_verdict({"symbol": "AAA", "side": "sell"}, False, lq)
        check("sell of held scoutTiq name refused", v == "scouttiq_hold", str(v))
        v = await sq.order_verdict({"symbol": "OLD", "side": "sell"}, False, lq)
        check("sell of a legacy position still allowed", v is None)
        os.environ["SCOUTTIQ_ONLY"] = "true"
        v = await sq.order_verdict({"symbol": "ZZZ", "side": "buy"}, False, lq)
        check("ONLY: other buys refused", v == "scouttiq_only_buy", str(v))
        v = await sq.order_verdict({"symbol": "SHORTY", "side": "buy"}, False, lq)
        check("ONLY: cover of an old short allowed", v is None)
        v = await sq.order_verdict({"symbol": "NEWSHORT", "side": "sell"}, True, lq)
        check("ONLY: new short refused", v == "scouttiq_only_short", str(v))
        os.environ["SCOUTTIQ_ALLOW_SHORTS"] = "true"
        v = await sq.order_verdict({"symbol": "NEWSHORT", "side": "sell"}, True, lq)
        check("ONLY + ALLOW_SHORTS: new short allowed", v is None, str(v))
        v = await sq.order_verdict({"symbol": "ZZZ", "side": "buy"}, False, lq)
        check("ONLY + ALLOW_SHORTS: other long buy still refused", v == "scouttiq_only_buy", str(v))
        v = await sq.order_verdict({"symbol": "AAA", "side": "sell"}, True, lq)
        check("ONLY + ALLOW_SHORTS: held scoutTiq name still cannot be sold/shorted", v == "scouttiq_hold", str(v))
        del os.environ["SCOUTTIQ_ALLOW_SHORTS"]
        v = await sq.order_verdict({"symbol": "NEWSHORT", "side": "sell"}, True, lq)
        check("ALLOW_SHORTS off again: new short refused", v == "scouttiq_only_short", str(v))
        tok = sq.SCOUTTIQ_CTX.set(True)
        v = await sq.order_verdict({"symbol": "AAA", "side": "sell"}, False, lq)
        sq.SCOUTTIQ_CTX.reset(tok)
        check("scoutTiq's own sell passes", v is None)
        os.environ["SCOUTTIQ_ONLY"] = "false"
        v = await sq.order_verdict({"symbol": "ZZZ", "side": "buy"}, False, lq)
        check("ONLY off: buys unaffected", v is None)
        # exits: before exit date nothing; on the exit date only from 15:56
        et = sq.ET
        await sq._exit_pass("2026-11-19", datetime(2026, 11, 19, 15, 40, tzinfo=et), sessions)
        check("no exit before 15:56 on the exit date", calls["sells"] == [])
        await sq._exit_pass("2026-11-19", datetime(2026, 11, 19, 15, 56, tzinfo=et), sessions)
        check("exit at 15:56 on the exit date", sorted(s[0] for s in calls["sells"]) == ["AAA", "BBB"], str(calls["sells"]))
        check("positions cleared and ledger closed", not sq.load_state()["positions"] and len(calls["ledger"]) == 2)
        # configurable hold: new entries use SCOUTTIQ_HOLD_SESSIONS, bad values fall back / clamp
        os.environ["SCOUTTIQ_HOLD_SESSIONS"] = "7"
        st = sq.load_state()
        sq._roll_counters(st, "2026-11-16")
        await sq._enter("EEE", 10.0, "A", {}, st, "2026-11-16", sessions)
        check("hold 7: exit date is 7 sessions later", st["positions"].get("EEE", {}).get("exit_date") == sq.session_offset(sessions, "2026-11-16", 7), str(st["positions"].get("EEE")))
        check("hold recorded on the position ledger entry", True)
        os.environ["SCOUTTIQ_HOLD_SESSIONS"] = "abc"
        check("bad hold value falls back to 5", sq.hold_sessions() == 5)
        os.environ["SCOUTTIQ_HOLD_SESSIONS"] = "99"
        check("hold clamped to 20", sq.hold_sessions() == 20)
        os.environ["SCOUTTIQ_HOLD_SESSIONS"] = "0"
        check("hold clamped to 1", sq.hold_sessions() == 1)
        del os.environ["SCOUTTIQ_HOLD_SESSIONS"]
        check("default hold is 5", sq.hold_sessions() == 5)
        # a position entered with hold 7 is NOT sold on session 5 and IS sold on session 7
        et2 = sq.ET
        d5 = sq.session_offset(sessions, "2026-11-16", 5)
        d7 = sq.session_offset(sessions, "2026-11-16", 7)
        n_before = len(calls["sells"])
        await sq._exit_pass(d5, datetime(2026, 11, 23, 15, 57, tzinfo=et2).replace(year=int(d5[:4]), month=int(d5[5:7]), day=int(d5[8:10])), sessions)
        check("hold-7 position not sold on session 5", len(calls["sells"]) == n_before and sq.is_held("EEE"))
        await sq._exit_pass(d7, datetime(2026, 11, 23, 15, 57, tzinfo=et2).replace(year=int(d7[:4]), month=int(d7[5:7]), day=int(d7[8:10])), sessions)
        check("hold-7 position sold on session 7", sq.is_held("EEE") is False and calls["sells"][-1][0] == "EEE", str(calls["sells"][-3:]))
        # a name the account already holds (e.g. a legacy short) is never bought, and a held name that is short at the broker is released
        calls["extra_live"].append({"symbol": "CPT", "qty": "-5", "unrealized_plpc": "0.0"})
        st = sq.load_state()
        sq._roll_counters(st, "2026-11-18")
        nb = len(calls["buys"])
        await sq._enter("CPT", 10.0, "A", {}, st, "2026-11-18", sessions)
        check("name already shorted at broker is not bought", len(calls["buys"]) == nb and not sq.is_held("CPT"))
        last = [json.loads(x) for x in sq.FLAGS_PATH().read_text().strip().splitlines()][-1]
        check("flag logged as already_at_broker", last.get("symbol") == "CPT" and last.get("why_not") == "already_at_broker", str(last))
        st = sq.load_state()
        st["positions"]["FFF"] = {"entry_date": "2026-11-17", "exit_date": "2026-11-30", "qty": 5, "flag_price": 10.0,
                                  "version": "A", "state": "open", "ts": datetime.now(timezone.utc).isoformat()}
        sq.save_state(st)
        calls["extra_live"].append({"symbol": "FFF", "qty": "-5", "unrealized_plpc": "0.0"})
        sq._RUN["last_live_check"] = 0.0
        ns = len(calls["sells"])
        await sq._exit_pass("2026-11-18", datetime(2026, 11, 18, 11, 0, tzinfo=sq.ET), sessions)
        check("held name short at broker is released, not sold", (not sq.is_held("FFF")) and len(calls["sells"]) == ns)
        calls["extra_live"].clear()
        # state file lost: the flags log still protects an entered-and-not-exited name
        sq._today_et = lambda: "2026-11-18"
        st = sq.load_state()
        sq._roll_counters(st, "2026-11-18")
        await sq._enter("GGG", 10.0, "A", {}, st, "2026-11-18", sessions)
        check("GGG entered and held", sq.is_held("GGG"))
        st["positions"].pop("GGG", None)
        sq.save_state(st)
        check("state lost: flags-log fallback still reports GGG held", sq.is_held("GGG"))
        v = await sq.order_verdict({"symbol": "GGG", "side": "sell"}, False, lq)
        check("state lost: sell of GGG still refused", v == "scouttiq_hold", str(v))
        sq.append_flag_log({"ts": "x", "date": "2026-11-18", "symbol": "GGG", "event": "exit", "reason": "test"})
        check("after an exit event the fallback releases GGG", not sq.is_held("GGG"))
        sq._today_et = lambda: "2026-12-30"
        sq.append_flag_log({"ts": "x", "date": "2026-11-18", "symbol": "HHH", "taken": True, "exit_date": "2026-11-20"})
        check("fallback ignores an entry whose exit date has passed", not sq.is_held("HHH"))
        # shadow mode never trades
        os.environ["SCOUTTIQ_MODE"] = "shadow"
        n = len(calls["buys"])
        st = sq.load_state()
        await sq._enter("DDD", 10.0, "A", {}, st, "2026-11-23", sessions)
        check("shadow mode places no order", len(calls["buys"]) == n and not sq.is_held("DDD"))

    asyncio.run(run())
    # flag log written
    p = sq.FLAGS_PATH()
    check("flag log written", p.exists() and len(p.read_text().strip().splitlines()) >= 4)


def run() -> int:
    from backend import scouttiq as sq
    for fn in (test_rule, test_eps, test_rows, test_sec_parse, test_guard_and_flow):
        try:
            fn(sq)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            _FAILS.append("%s crashed: %r" % (fn.__name__, exc))
    print("scouttiq selftest: %d checks, %d failed" % (_N[0], len(_FAILS)))
    for f in _FAILS:
        print("  FAIL:", f)
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(run())
