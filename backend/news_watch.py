"""
Breaking-news watcher for HELD positions (exit-only).

Why this exists: the main bot only looks at news as one input to its slow,
full-universe entry scan (1-2h per pass, regular hours only), so nothing was
watching the stocks we already hold. A clinical-trial failure, an SEC probe or
a buyout headline landing in premarket / after-hours was invisible until the
next scan -- and the resting trailing stops can't fire outside regular hours
anyway (Alpaca only triggers stop orders in the 9:30-16:00 ET session).

What it does, every NEWS_WATCH_INTERVAL_SECONDS (default 60) between
4:00 and 20:00 ET on weekdays:
  1. Pull Alpaca news for every symbol currently held (one batched request).
  2. Classify each NEW article against each held symbol:
       - long  + strongly NEGATIVE news  -> exit
       - short + strongly POSITIVE news  -> exit (takeover / approval / squeeze)
     "Strongly" = a curated catastrophic-phrase hit, or FinBERT >= NEWS_WATCH_MIN_CONF
     on the headline+summary. Articles tagging more than NEWS_WATCH_MAX_TAGGED
     symbols (market roundups) are ignored.
  3. Exit:
       - regular hours: cancel resting exits, market sell / cover.
       - premarket / after-hours: cancel resting exits, send an extended-hours
         marketable LIMIT (day, extended_hours=true) priced NEWS_EXIT_LIMIT_SLIP_PCT
         through the last price, repriced deeper every NEWS_EXIT_REPRICE_SECONDS
         if unfilled; converted to a plain market exit the moment the regular
         session opens.
Premarket gap guard (same exit machinery): between 4:00 and 9:30 ET a held position
whose price is GAP_GUARD_PCT (default 7%) or more AGAINST it versus the prior close
(down for a long, up for a short) on GAP_GUARD_CONFIRM_POLLS consecutive polls is
exited with the same extended-hours limit. If it hasn't filled by the open and the
move has retraced to under half the threshold, the exit is cancelled and a trailing
stop is put back instead of dumping into a recovered open; otherwise it becomes a
market exit at the open. Premarket only: after hours there is no clean "prior close"
to measure against (the day's own move would trip it), so after-hours risk is
covered by the news triggers alone.
This never opens or adds to a position. Set NEWS_WATCH_DRY_RUN=true to log the
decisions without placing any order, NEWS_WATCH_ENABLED=false to turn it off.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend import config
from backend.execution import (
    _cancel_all_open_orders_for_side,
    _cancel_all_open_sell_orders,
    _get_open_positions,
    _is_regular_market_hours,
    _open_orders_on_side,
    _post_order,
    _request,
    place_market_cover,
    place_market_sell,
    place_trailing_stop_buy,
    place_trailing_stop_sell,
)

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _cfg_f(name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except Exception:
        return default


def _cfg_b(name: str, default: bool) -> bool:
    val = getattr(config, name, default)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


# ── phrase lists ─────────────────────────────────────────────────────────────
# Past-tense / definitive phrasings only. Press releases end with forward-looking
# boilerplate full of "may fail to..." language, so article bodies are cut off at
# that section before matching (see _clean_text).
_NEG_PATTERNS = [
    r"did not (meet|achieve|reach) (its |the |their )?(primary|co-primary)",
    r"(failed|fails) to (meet|achieve|demonstrate|show|reach) ",
    r"missed (its |the |their )?(primary|co-primary) (endpoint|objective)",
    r"not statistically significant",
    r"(trial|study|program|phase \d\w*)[^.]{0,60}\b(failed|fails|did not succeed)\b",
    r"discontinu(e|es|ed|ing) (the )?(development|program|trial|study|clinical)",
    r"clinical hold",
    r"complete response letter",
    r"\bgoing concern\b",
    r"chapter 11",
    r"files? for bankruptcy|filed for bankruptcy",
    r"delist(ed|ing)? (notice|from)|notice of delisting",
    r"\bsubpoena",
    r"(sec|doj|justice department|department of justice)[^.]{0,30}(investigation|inquiry|probe|charges|charged|subpoena)",
    r"\brestate(d|ment|s)?\b",
    r"(securities|accounting|wire|bank) fraud|alleg\w* [^.]{0,40}fraud|accused of [^.]{0,30}fraud|charged with [^.]{0,30}fraud",
    r"accounting (irregularit|error|fraud)",
    r"auditor (resign|dismiss)",
    r"(withdraws?|withdrew|suspends?) (its )?(full-year |annual )?(financial )?(guidance|outlook)",
    r"(cuts?|lowers?|slashes|reduces?) (its )?(full-year |annual |fy ?\d{2,4} )?(revenue |earnings |eps )?(guidance|outlook|forecast)",
    r"short[- ]seller report",
    r"voluntary recall|(announces?|issues?|initiates?|expands?) [^.]{0,30}\brecall\b",
    r"(announces?|prices?|priced|pricing) [^.]{0,60}(underwritten|public) offering",
    r"material weakness",
    r"ceo (resigns|steps down|terminated|fired)",
]
_POS_PATTERNS = [
    r"(to be|being|will be) acquired",
    r"agreement to (be )?(acquire|acquired|merge)",
    r"definitive (merger |acquisition )?agreement",
    r"tender offer",
    r"\bbuyout\b|\btakeover\b|\btake[- ]private\b",
    r"fda (approves|approval|approved|grants approval)",
    r"(met|meets|achieved|achieves) (its |the |their )?(primary|co-primary) (endpoint|objective)",
    r"positive (topline|top-line|phase \d\w*|pivotal)",
    r"(raises?|raised|increases?) (its )?(full-year |annual )?(revenue |earnings |eps )?(guidance|outlook)",
    r"strategic alternatives",
    r"short squeeze",
]
_NEG_RX = [re.compile(p, re.I) for p in _NEG_PATTERNS]
_POS_RX = [re.compile(p, re.I) for p in _POS_PATTERNS]
_CUT_RX = re.compile(r"forward[- ]looking statements?|safe harbor", re.I)
_TAG_RX = re.compile(r"<[^>]+>")


def _clean_text(article: dict[str, Any]) -> str:
    headline = str(article.get("headline") or "")
    summary = str(article.get("summary") or "")
    content = str(article.get("content") or "")
    body = html.unescape(_TAG_RX.sub(" ", content))
    cut = _CUT_RX.search(body)
    if cut:
        body = body[: cut.start()]
    body = re.sub(r"\s+", " ", body)[:3000]
    return f"{headline}. {summary}. {body}".strip()


def _phrase_hit(text: str, patterns: list[re.Pattern]) -> Optional[str]:
    for rx in patterns:
        m = rx.search(text)
        if m:
            return m.group(0)[:80]
    return None


async def _finbert(text: str) -> tuple[str, float]:
    """(label, confidence) for one text via the same FinBERT the scan uses."""
    try:
        from backend.services.newsservice import _service

        out = await _service._score_headlines_finbert([text[:500]])
        return str(out.get("label", "neutral")).lower(), float(out.get("confidence", 0.0) or 0.0)
    except Exception:
        logger.exception("news_watch: FinBERT scoring failed")
        return "neutral", 0.0


async def classify(article: dict[str, Any], is_short: bool) -> Optional[str]:
    """Return a short reason string if this article should trigger an exit."""
    text = _clean_text(article)
    if is_short:
        hit = _phrase_hit(text, _POS_RX)
        if hit:
            return f"phrase:'{hit}'"
        label, conf = await _finbert(text)
        if label == "positive" and conf >= _cfg_f("NEWS_WATCH_MIN_CONF", 0.90):
            return f"finbert positive {conf:.2f}"
        return None
    hit = _phrase_hit(text, _NEG_RX)
    if hit:
        return f"phrase:'{hit}'"
    label, conf = await _finbert(text)
    if label == "negative" and conf >= _cfg_f("NEWS_WATCH_MIN_CONF", 0.90):
        return f"finbert negative {conf:.2f}"
    return None


# ── state ────────────────────────────────────────────────────────────────────
_seen_ids: set[str] = set()
# symbol -> {"ts": first trigger, "last": last order time, "attempt": n, "reason": str}
_actions: dict[str, dict[str, Any]] = {}
_last_poll: Optional[datetime] = None
_gap_hits: dict[str, int] = {}


def _is_premarket(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 4 * 60 <= minutes < 9 * 60 + 30


def _against_move(pos: dict[str, Any]) -> Optional[float]:
    """Fractional move of the price vs the prior close, signed so a positive
    number is bad for the position (down for a long, up for a short)."""
    try:
        cur = float(pos.get("current_price") or 0)
        prev = float(pos.get("lastday_price") or 0)
        qty = float(pos.get("qty") or 0)
    except Exception:
        return None
    if cur <= 0 or prev <= 0 or qty == 0:
        return None
    chg = cur / prev - 1.0
    return -chg if qty > 0 else chg


async def _restore_protection(symbol: str, qty: float) -> None:
    """Cancel leftover premarket exit orders and put a trailing stop back."""
    if _cfg_b("NEWS_WATCH_DRY_RUN", False):
        logger.warning("GAP GUARD %s recovered — would restore trailing stop [DRY RUN]", symbol)
        return
    trail = _cfg_f("GAP_RESTORE_TRAIL_PCT", 2.0)
    tid = None
    try:
        if qty < 0:
            if await _cancel_all_open_orders_for_side(symbol, "buy"):
                tid = await place_trailing_stop_buy(symbol, abs(qty), trail)
        else:
            await _cancel_all_open_sell_orders(symbol)
            tid = await place_trailing_stop_sell(symbol, qty, trail)
    except Exception:
        logger.exception("news_watch: restoring protection failed for %s", symbol)
    if tid:
        logger.warning("GAP GUARD %s recovered by the open — premarket exit cancelled, trailing stop %.1f%% restored id=%s", symbol, trail, tid)
    else:
        logger.error("GAP GUARD %s — could not restore trailing stop; _protect_positions will retry", symbol)


def _in_watch_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


async def _fetch_news(symbols: list[str], start: datetime) -> list[dict[str, Any]]:
    base = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
    out: list[dict[str, Any]] = []
    for i in range(0, len(symbols), 40):
        chunk = symbols[i : i + 40]
        token = None
        for _ in range(3):
            params = {
                "symbols": ",".join(chunk),
                "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "limit": 50,
                "sort": "desc",
                "include_content": "true",
            }
            if token:
                params["page_token"] = token
            resp = await _request("GET", f"{base}/v1beta1/news", params=params)
            if resp.status_code != 200:
                logger.warning("news_watch: news fetch %s %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            out.extend(a for a in (data.get("news") or []) if isinstance(a, dict))
            token = data.get("next_page_token")
            if not token:
                break
    return out


async def _extended_exit(symbol: str, qty: float, price: float, attempt: int) -> Optional[str]:
    is_short = qty < 0
    shares = abs(qty)
    shares_s = str(int(shares)) if float(shares).is_integer() else str(shares)
    slip = _cfg_f("NEWS_EXIT_LIMIT_SLIP_PCT", 0.03) * min(attempt + 1, 4)
    if is_short:
        side, limit = "buy", round(price * (1.0 + slip), 2)
        freed = await _cancel_all_open_orders_for_side(symbol, "buy")
    else:
        side, limit = "sell", round(price * (1.0 - slip), 2)
        await _cancel_all_open_sell_orders(symbol)
        freed = True
    if not freed:
        logger.warning("news_watch: %s exits still holding shares — will retry", symbol)
        return None
    result = await _post_order({
        "symbol": symbol,
        "qty": shares_s,
        "side": side,
        "type": "limit",
        "limit_price": str(limit),
        "time_in_force": "day",
        "extended_hours": True,
    })
    return result.get("id") if result else None


async def _regular_exit(symbol: str, qty: float) -> Optional[str]:
    # A market exit already working (e.g. the stock is halted) must not be
    # cancelled and re-sent every cycle — just leave it.
    side = "buy" if qty < 0 else "sell"
    for o in await _open_orders_on_side(symbol, side):
        if str(o.get("type", "")).lower() == "market":
            return str(o.get("id") or "existing")
    if qty < 0:
        return await place_market_cover(symbol, abs(qty))
    await _cancel_all_open_sell_orders(symbol)
    return await place_market_sell(symbol, qty)


async def _do_exit(symbol: str, qty: float, price: float, state: dict[str, Any]) -> None:
    dry = _cfg_b("NEWS_WATCH_DRY_RUN", False)
    regular = _is_regular_market_hours()
    mode = "market" if regular else f"extended-hours limit (attempt {state['attempt'] + 1})"
    tag = "GAP" if state.get("kind") == "gap" else "NEWS"
    logger.warning(
        "%s EXIT %s qty=%s px=%.2f via %s | reason=%s%s",
        tag, symbol, qty, price, mode, state["reason"], " [DRY RUN]" if dry else "",
    )
    if dry:
        state["last"] = time.time()
        state["attempt"] += 1
        state["regular"] = regular
        return
    try:
        oid = await (_regular_exit(symbol, qty) if regular else _extended_exit(symbol, qty, price, state["attempt"]))
    except Exception:
        logger.exception("news_watch: exit failed for %s", symbol)
        oid = None
    state["last"] = time.time()
    state["attempt"] += 1
    state["regular"] = regular
    if oid:
        logger.warning("%s EXIT %s order placed id=%s", tag, symbol, oid)
    else:
        logger.error("%s EXIT %s order NOT placed — will retry next cycle", tag, symbol)


async def _poll_once() -> None:
    global _last_poll
    positions = await _get_open_positions()
    held: dict[str, dict[str, Any]] = {}
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        try:
            q = float(p.get("qty") or 0)
        except Exception:
            q = 0.0
        if sym and q != 0:
            held[sym] = p

    # drop actions for positions that are gone (exit filled)
    for sym in [s for s in _actions if s not in held]:
        logger.warning("%s EXIT %s — position closed", "GAP" if _actions[sym].get("kind") == "gap" else "NEWS", sym)
        _actions.pop(sym, None)
    if not held:
        _last_poll = datetime.now(timezone.utc)
        return

    now_utc = datetime.now(timezone.utc)
    start = (_last_poll - timedelta(minutes=2)) if _last_poll else (
        now_utc - timedelta(minutes=_cfg_f("NEWS_WATCH_LOOKBACK_MINUTES", 30))
    )
    articles = await _fetch_news(sorted(held), start)
    _last_poll = now_utc

    max_tagged = int(_cfg_f("NEWS_WATCH_MAX_TAGGED", 4))
    for art in sorted(articles, key=lambda a: str(a.get("created_at") or "")):
        aid = str(art.get("id") or "")
        if not aid or aid in _seen_ids:
            continue
        _seen_ids.add(aid)
        tagged = [str(s).upper() for s in (art.get("symbols") or [])]
        if not tagged or len(tagged) > max_tagged:
            continue
        for sym in tagged:
            pos = held.get(sym)
            if not pos or sym in _actions:
                continue
            qty = float(pos.get("qty") or 0)
            reason = await classify(art, is_short=qty < 0)
            if not reason:
                continue
            logger.warning(
                "NEWS TRIGGER %s (%s) | %s | headline=%r",
                sym, "short" if qty < 0 else "long", reason, str(art.get("headline"))[:140],
            )
            _actions[sym] = {"ts": time.time(), "last": 0.0, "attempt": 0, "reason": reason, "regular": False, "kind": "news"}
    if len(_seen_ids) > 5000:
        _seen_ids.clear()

    # premarket gap guard
    if _cfg_b("GAP_GUARD_ENABLED", True) and _is_premarket(datetime.now(_ET)):
        gap_pct = _cfg_f("GAP_GUARD_PCT", 0.07)
        need = max(1, int(_cfg_f("GAP_GUARD_CONFIRM_POLLS", 2)))
        for sym, pos in held.items():
            if sym in _actions:
                continue
            mv = _against_move(pos)
            if mv is None or mv < gap_pct:
                _gap_hits.pop(sym, None)
                continue
            _gap_hits[sym] = _gap_hits.get(sym, 0) + 1
            if _gap_hits[sym] >= need:
                qty = float(pos.get("qty") or 0)
                reason = f"premarket gap {mv:.1%} against {'short' if qty < 0 else 'long'} vs prior close"
                logger.warning("GAP TRIGGER %s | %s (confirmed %d polls)", sym, reason, _gap_hits[sym])
                _actions[sym] = {"ts": time.time(), "last": 0.0, "attempt": 0, "reason": reason, "regular": False, "kind": "gap"}
    else:
        _gap_hits.clear()

    # place / re-place exits for everything triggered and still held
    reprice = _cfg_f("NEWS_EXIT_REPRICE_SECONDS", 120)
    regular = _is_regular_market_hours()
    for sym, state in list(_actions.items()):
        pos = held.get(sym)
        if not pos:
            continue
        qty = float(pos.get("qty") or 0)
        try:
            price = float(pos.get("current_price") or 0)
        except Exception:
            price = 0.0
        # an extended-hours limit left over when the regular session opens
        # becomes a market exit (fresh retry budget), however many times it
        # was repriced overnight
        convert = regular and not state.get("regular")
        if convert and state.get("kind") == "gap":
            mv = _against_move(pos)
            if mv is None or mv < _cfg_f("GAP_GUARD_PCT", 0.07) / 2:
                await _restore_protection(sym, qty)
                _actions.pop(sym, None)
                continue
        if convert:
            state["attempt"] = 0
        elif state["attempt"] >= 8:
            continue  # give up; _protect_positions restores a stop at the next regular session
        due = convert or state["attempt"] == 0 or (time.time() - state["last"]) >= reprice
        if due and price > 0:
            await _do_exit(sym, qty, price, state)


async def monitor_held_news() -> None:
    """Background task: start once from main.py."""
    if not _cfg_b("NEWS_WATCH_ENABLED", True):
        logger.info("news_watch disabled (NEWS_WATCH_ENABLED=false)")
        return
    interval = max(15.0, _cfg_f("NEWS_WATCH_INTERVAL_SECONDS", 60))
    logger.info(
        "news_watch started — interval=%.0fs dry_run=%s window=04:00-20:00 ET weekdays",
        interval, _cfg_b("NEWS_WATCH_DRY_RUN", False),
    )
    while True:
        try:
            if _in_watch_window(datetime.now(_ET)):
                await _poll_once()
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("news_watch loop error")
            await asyncio.sleep(interval)
