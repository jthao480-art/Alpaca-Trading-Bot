"""
Positive-news BUY signal (entries), the mirror image of news_watch.py.

news_watch.py only protects positions we already hold. This module watches the
whole Alpaca news stream (every symbol, not just holdings) 4:00-19:30 ET on
trading days and buys a stock when a fresh, single-company article is a strong
catalyst -- including in pre-market and after-hours, where the main scan never
trades.

Two tiers
  A  "catalyst": a curated, definitive-phrase hit (FDA approval, primary
     endpoint met, raised guidance, earnings beat, record results, large
     contract win, ...) and FinBERT does not call the text negative.
     Full size (NEWS_BUY_POSITION_PCT of equity).
  B  "wide net": no phrase hit, but FinBERT says positive with confidence
     >= NEWS_BUY_WIDE_MIN_CONF AND price already confirms it (up at least
     NEWS_BUY_WIDE_MIN_MOVE_PCT vs the prior close). Reduced size
     (NEWS_BUY_WIDE_SIZE_MULT). Set NEWS_BUY_WIDE_ENABLED=false for A only.

  C  "social-led": a ticker that is suddenly hot on Reddit (ApeWisdom) or on
     StockTwits trending (backend/social_buzz.py) AND has a recent positive
     Alpaca article (FinBERT >= NEWS_BUY_SOCIAL_MIN_CONF, no negatives) AND is
     already up >= NEWS_BUY_SOCIAL_MIN_MOVE_PCT. Social buzz alone never buys --
     it has to be backed by real news. Half size, NEWS_BUY_SOCIAL_MAX_PER_DAY.
  Social also acts on A/B: a clearly bearish StockTwits crowd vetoes the buy
  (sell-the-news), a hot ticker gets NEWS_BUY_SOCIAL_BOOST_MULT size on tier A
  and can confirm tier B in place of a +1% price move (needs price >= 0%).
  Set NEWS_BUY_SOCIAL_ENABLED=false / NEWS_BUY_SOCIAL_LED_ENABLED=false to drop it.

Filters (all must pass) -- the point is to avoid chasing and to avoid junk
  * article <= NEWS_BUY_MAX_AGE_MINUTES old, tags <= NEWS_BUY_MAX_TAGGED symbols
    (roundups ignored), symbol named in the text when two are tagged
  * not a takeover target (price is pinned to the deal), not an offering /
    going-concern / probe etc. (news_watch's negative list), no negative
    article on the symbol in the last NEWS_BUY_NEG_LOOKBACK_HOURS
  * not analyst-rating / "shares are moving" fluff
  * last trade fresh, price >= MIN_PRICE, prior-day dollar volume >=
    NEWS_BUY_MIN_PREV_DOLLAR_VOL, spread <= NEWS_BUY_MAX_SPREAD_PCT (a quote is
    required outside regular hours), and not already up more than
    NEWS_BUY_MAX_CHASE_PCT vs the prior close
  * not held, not on ENTRY_BLOCKLIST, not closed within
    NEWS_BUY_REENTRY_HOURS, once per symbol per day, at most
    NEWS_BUY_MAX_PER_DAY per day, below MAX_POSITIONS, daily-loss halt not hit

Execution
  * regular hours: normal bracket buy with a NEWS_BUY_TRAIL_PCT trailing stop
    (same path the main bot uses, so protection/swap logic applies)
  * pre/after-market: marketable LIMIT (extended_hours, day) NEWS_BUY_EXT_LIMIT_SLIP_PCT
    above the last price, unfilled remainder cancelled after
    NEWS_BUY_EXT_FILL_WAIT_SECONDS. Stops don't trigger outside regular hours,
    so a GTC hard stop is parked (active from the open) and the trailing stop
    is queued for the open via the existing deferred-stop mechanism.
  * ledger entry strategy "newsbuy" (excluded from the main bot's daily cap;
    it has its own NEWS_BUY_MAX_PER_DAY) with the headline in metadata.

Kill switches: NEWS_BUY_ENABLED=false turns it off; NEWS_BUY_DRY_RUN=true logs
the decisions without placing orders.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend import config
from backend import execution as _ex
from backend.execution import (
    _buy_risk_check,
    _cancel_order_by_id,
    _daily_loss_halt,
    _get_account,
    _get_open_positions,
    _get_order_by_id,
    _is_regular_market_hours,
    _post_order,
    _request,
    place_bracket_buy,
    place_stop_loss_sell,
)
from backend.news_watch import (
    _NEG_RX,
    _clean_text,
    _fetch_news,
    _finbert,
    _phrase_hit,
)
from backend.services.trade_ledger_service import add_entry, load_ledger, save_ledger

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


# ── classification ───────────────────────────────────────────────────────────
_BUY_PATTERNS = [
    r"fda (approves|approved|grants? (full |accelerated |marketing |priority )?approval|clears|cleared)",
    r"receives? fda (approval|clearance)|(granted|receives?|receives?d) [^.]{0,30}breakthrough (therapy|device )?designation",
    r"(met|meets|achieved|achieves|hit|hits) (its |the |their )?(primary|co-primary) (endpoint|objective)",
    r"positive (topline|top-line|phase \d\w*|pivotal|interim) (results|data|readout)",
    r"(raises?|raised|increases?|boosts?|lifts?|hikes?) (its |their )?(full-year |annual |fy ?\d{2,4} |fiscal (year )?\d{2,4} )?(revenue |earnings |eps |sales |profit |ebitda )?(guidance|outlook|forecast)",
    r"(beats?|tops?|exceeds?|surpass(es)?|crushes) (analyst |wall street |consensus |street )?(estimates|expectations|forecasts)",
    r"record (first[- ]quarter |second[- ]quarter |third[- ]quarter |fourth[- ]quarter |quarterly |annual |full[- ]year )?(revenue|earnings|sales|net income|bookings)",
    r"(awarded|wins?|won|secures?|secured|lands?) [^.]{0,60}(contract|order)s? (valued|worth) [^.]{0,15}\$\s?\d[\d.,]*\s?(million|billion|m\b|b\b)",
    r"(authorizes?|approves?|announces?) [^.]{0,40}(share |stock )?(repurchase|buyback)[^.]{0,40}\$\s?\d",
]
_BUY_RX = [re.compile(p, re.I) for p in _BUY_PATTERNS]

# Takeover targets trade at the deal price -- nothing left to capture.
_TARGET_RX = re.compile(
    r"(to be|being|will be) acquired|agreement to (be )?(acquire|acquired|merge)|"
    r"definitive (merger |acquisition )?agreement|tender offer|\bbuyout\b|take[- ]private|"
    r"merger agreement|go(es|ing)? private",
    re.I,
)
# Lagging "why is it moving" notes and analyst-rating headlines are not catalysts.
_FLUFF_RX = re.compile(
    r"price target|analyst|upgrade|downgrade|initiates? coverage|reiterat|"
    r"why is|why are|shares are (trading|moving|surging|jumping)|stocks? (are )?(moving|surging|jumping)|"
    r"top gainers|biggest movers|movers|unusual options|options activity|"
    r"what('s| is) going on|is it time to|should you|here('s| is) why|\bwatch\b|"
    r"\bhow (to|much)\b|ahead of (q\d|earnings|the)|what to expect|earnings preview|preview:|"
    r"\ba (month|year|week) (from|with|by)\b|passive income|dividend (stock|income|yield)|best (stocks|etfs)|"
    r"stocks? to (buy|watch)|buy now|buy or sell|should i|prediction|could (soar|surge|rally|double)|"
    r"\bif you (invested|had)\b|\bwhat (is|are)\b|\bwhy (is|are)\b",
    re.I,
)


async def classify_buy(article: dict[str, Any], wide_conf: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Return {"tier": "A"|"B", "reason": str, "conf": float} or None.
    wide_conf overrides the FinBERT-only confidence bar (used by the social-led path)."""
    headline = str(article.get("headline") or "")
    text = _clean_text(article)
    if _FLUFF_RX.search(headline):
        return None
    if _TARGET_RX.search(text):
        return None
    neg = _phrase_hit(text, _NEG_RX)
    if neg:
        return None
    hit = _phrase_hit(text, _BUY_RX)
    label, conf = await _finbert(text)
    if hit:
        if label == "negative" and conf >= 0.6:
            return None
        return {"tier": "A", "reason": f"phrase:'{hit}'", "conf": conf if label == "positive" else 0.0}
    if wide_conf is not None or _cfg_b("NEWS_BUY_WIDE_ENABLED", True):
        if label == "positive" and conf >= (wide_conf if wide_conf is not None else _cfg_f("NEWS_BUY_WIDE_MIN_CONF", 0.95)):
            return {"tier": "B", "reason": f"finbert positive {conf:.2f}", "conf": conf}
    return None


# ── market data / eligibility ────────────────────────────────────────────────
def _parse_ts(s: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


_VALID_SYM = re.compile(r"^[A-Z]{1,5}$")
_calendar_cache: dict[str, bool] = {}
_asset_cache: dict[str, bool] = {}


async def _is_trading_day(now: datetime) -> bool:
    key = now.strftime("%Y-%m-%d")
    if key in _calendar_cache:
        return _calendar_cache[key]
    ok = now.weekday() < 5
    try:
        resp = await _request(
            "GET",
            f"{str(getattr(config, 'ALPACA_BASE_URL', '')).rstrip('/')}/v2/calendar",
            params={"start": key, "end": key},
        )
        if resp.status_code == 200:
            data = resp.json()
            ok = bool(isinstance(data, list) and data and str(data[0].get("date")) == key)
    except Exception:
        logger.exception("news_buy: calendar lookup failed")
    _calendar_cache[key] = ok
    return ok


async def _asset_ok(symbol: str) -> bool:
    if symbol in _asset_cache:
        return _asset_cache[symbol]
    ok = False
    try:
        resp = await _request("GET", f"{str(getattr(config, 'ALPACA_BASE_URL', '')).rstrip('/')}/v2/assets/{symbol}")
        if resp.status_code == 200:
            a = resp.json()
            ok = (
                bool(a.get("tradable"))
                and str(a.get("status", "")).lower() == "active"
                and str(a.get("class", "us_equity")).lower() == "us_equity"
                and str(a.get("exchange", "")).upper() not in ("OTC", "")
            )
    except Exception:
        logger.exception("news_buy: asset lookup failed for %s", symbol)
    _asset_cache[symbol] = ok
    return ok


async def _snapshot(symbol: str) -> Optional[dict[str, Any]]:
    """price, prior close, change vs prior close, spread, prior dollar volume."""
    base = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
    s = None
    try:
        for attempt in range(3):
            resp = await _request("GET", f"{base}/v2/stocks/{symbol}/snapshot", params={"feed": "iex"})
            if resp.status_code == 200:
                s = resp.json()
                break
            if resp.status_code == 429 and attempt < 2:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            logger.info("news_buy: snapshot %s -> HTTP %s %s", symbol, resp.status_code, resp.text[:120])
            return None
    except Exception:
        logger.exception("news_buy: snapshot failed for %s", symbol)
        return None
    if not isinstance(s, dict):
        return None
    trade = s.get("latestTrade") or {}
    price = float(trade.get("p") or 0)
    tdt = _parse_ts(trade.get("t"))
    if price <= 0 or tdt is None:
        logger.info("news_buy: snapshot %s has no usable latest trade (%s)", symbol, trade)
        return None
    today = datetime.now(_ET).date()
    daily, prev = s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
    ref = None
    ddt = _parse_ts(daily.get("t")) if daily else None
    if daily and ddt is not None and ddt.astimezone(_ET).date() < today:
        ref = daily
    elif prev:
        ref = prev
    if not ref or not float(ref.get("c") or 0):
        logger.info("news_buy: snapshot %s has no prior daily bar", symbol)
        return None
    ref_close = float(ref["c"])
    quote = s.get("latestQuote") or {}
    bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
    spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100.0) if bid > 0 and ask > 0 and ask >= bid else None
    return {
        "price": price,
        "trade_age_s": (datetime.now(timezone.utc) - tdt).total_seconds(),
        "ref_close": ref_close,
        "chg_pct": (price / ref_close - 1.0) * 100.0,
        "spread_pct": spread_pct,
        "bid": bid,
        "ask": ask,
        "prev_dollar_vol": ref_close * float(ref.get("v") or 0),
    }


def _ledger_checks(ledger: dict[str, Any], symbol: str, tier: str = "A") -> Optional[str]:
    """Return a reason string if the ledger forbids a buy, else None."""
    now = datetime.now(timezone.utc)
    today = datetime.now(_ET).date()
    n_today = 0
    n_social = 0
    for sym, entries in ledger.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            ca = _parse_ts(e.get("created_at"))
            if str(e.get("strategy", "")).lower() == "newsbuy" and ca is not None and ca.astimezone(_ET).date() == today:
                n_today += 1
                if (e.get("metadata") or {}).get("tier") == "C":
                    n_social += 1
                if sym == symbol:
                    return "already news-bought today"
    cap = int(_cfg_f("NEWS_BUY_MAX_PER_DAY", 4))
    if cap > 0 and n_today >= cap:
        return f"daily news-buy cap {n_today}/{cap}"
    scap = int(_cfg_f("NEWS_BUY_SOCIAL_MAX_PER_DAY", 2))
    if tier == "C" and scap > 0 and n_social >= scap:
        return f"daily social-led cap {n_social}/{scap}"
    hours = _cfg_f("NEWS_BUY_REENTRY_HOURS", 12.0)
    for e in ledger.get(symbol, []) if isinstance(ledger.get(symbol), list) else []:
        if not isinstance(e, dict):
            continue
        if e.get("status") == "open":
            return "open ledger entry"
        ca = _parse_ts(e.get("closed_at"))
        if hours > 0 and ca is not None and (now - ca) < timedelta(hours=hours):
            return f"closed within {hours:g}h"
    return None


def _blocklisted(symbol: str) -> bool:
    raw = str(getattr(config, "ENTRY_BLOCKLIST", "") or "")
    return symbol in {s.strip().upper() for s in raw.split(",") if s.strip()}


async def _recent_negative(symbol: str) -> Optional[str]:
    hours = _cfg_f("NEWS_BUY_NEG_LOOKBACK_HOURS", 6.0)
    try:
        arts = await _fetch_news([symbol], datetime.now(timezone.utc) - timedelta(hours=hours))
    except Exception:
        return None
    for a in arts:
        hit = _phrase_hit(_clean_text(a), _NEG_RX)
        if hit:
            return f"recent negative news '{hit}'"
    return None


# ── execution ────────────────────────────────────────────────────────────────
_buy_lock = asyncio.Lock()


async def _extended_buy(symbol: str, qty: int, limit: float, trail: float) -> tuple[Optional[str], Optional[float], float]:
    """Marketable limit outside regular hours. Returns (order_id, avg_price, filled_qty)."""
    ok, reason, adj = await _buy_risk_check(symbol, qty, limit)
    if not ok or int(adj) < 1:
        logger.info("NEWS BUY %s skipped — risk check: %s", symbol, reason)
        return None, None, 0.0
    res = await _post_order({
        "symbol": symbol,
        "qty": str(int(adj)),
        "side": "buy",
        "type": "limit",
        "limit_price": str(limit),
        "time_in_force": "day",
        "extended_hours": True,
    })
    if not res or not res.get("id"):
        return None, None, 0.0
    oid = str(res["id"])
    wait = max(5.0, _cfg_f("NEWS_BUY_EXT_FILL_WAIT_SECONDS", 45.0))
    filled, avg, waited = 0.0, None, 0.0
    while waited < wait:
        await asyncio.sleep(3.0)
        waited += 3.0
        o = await _get_order_by_id(oid)
        if not o:
            continue
        filled = float(o.get("filled_qty") or 0)
        avg = float(o.get("filled_avg_price") or 0) or avg
        if str(o.get("status", "")).lower() in ("filled", "canceled", "expired", "rejected"):
            break
    if filled < int(adj):
        try:
            await _cancel_order_by_id(oid)
            await asyncio.sleep(1.0)
            o = await _get_order_by_id(oid)
            if o:
                filled = float(o.get("filled_qty") or 0)
                avg = float(o.get("filled_avg_price") or 0) or avg
        except Exception:
            logger.exception("news_buy: cancel of unfilled remainder failed for %s", symbol)
    if filled <= 0:
        logger.info("NEWS BUY %s — limit %.2f not filled in %.0fs, cancelled", symbol, limit, wait)
        return oid, None, 0.0
    # Stops don't trigger outside regular hours: park a GTC hard stop (active from the
    # open) and queue the trailing stop for the open via the deferred mechanism.
    try:
        _ex._deferred_trailing_stops[symbol] = trail
        _ex._save_deferred_stops()
    except Exception:
        logger.exception("news_buy: could not queue deferred trailing stop for %s", symbol)
    try:
        hard = _cfg_f("NEWS_BUY_HARD_STOP_PCT", 5.0)
        stop_px = round((avg or limit) * (1.0 - hard / 100.0), 2)
        await place_stop_loss_sell(symbol, filled, stop_px)
    except Exception:
        logger.exception("news_buy: hard stop placement failed for %s", symbol)
    return oid, avg, filled


async def _execute_buy(
    article: dict[str, Any], symbol: str, verdict: dict[str, Any], snap: dict[str, Any], buzz: Optional[dict[str, Any]] = None
) -> bool:
    headline = str(article.get("headline") or "")[:200]
    extended = not _is_regular_market_hours()
    acct = await _get_account()
    if not acct:
        return False
    equity = float(acct.get("equity") or 0)
    if verdict["tier"] == "A":
        mult = _cfg_f("NEWS_BUY_SOCIAL_BOOST_MULT", 1.25) if (buzz and buzz.get("hot")) else 1.0
    elif verdict["tier"] == "C":
        mult = _cfg_f("NEWS_BUY_SOCIAL_SIZE_MULT", 0.5)
    else:
        mult = _cfg_f("NEWS_BUY_WIDE_SIZE_MULT", 0.5)
    notional = equity * _cfg_f("NEWS_BUY_POSITION_PCT", 2.0) / 100.0 * mult
    notional = min(notional, _cfg_f("NEWS_BUY_MAX_POSITION_USD", 2000.0))
    ref_px = snap["price"] * (1.0 + _cfg_f("NEWS_BUY_EXT_LIMIT_SLIP_PCT", 0.5) / 100.0) if extended else snap["price"]
    qty = int(notional // ref_px)
    if qty < 1:
        logger.info("NEWS BUY %s skipped — size %.0f too small for price %.2f", symbol, notional, ref_px)
        return False
    trail = _cfg_f("NEWS_BUY_TRAIL_PCT", 2.0)
    tag = f"tier {verdict['tier']} {verdict['reason']}" + (f" | social: {buzz['reason']}" if buzz and buzz.get("hot") else "")
    if _cfg_b("NEWS_BUY_DRY_RUN", False):
        logger.warning(
            "NEWS BUY [DRY RUN] %s qty=%d @~%.2f (%s, %+.1f%% vs prior close, %s) | %s",
            symbol, qty, ref_px, tag, snap["chg_pct"], "extended" if extended else "regular", headline,
        )
        return True

    try:
        from backend.botv3 import _BOUGHT_THIS_SESSION, _PENDING_BUYS  # type: ignore
    except Exception:
        _BOUGHT_THIS_SESSION, _PENDING_BUYS = set(), set()
    _PENDING_BUYS.add(symbol)
    _BOUGHT_THIS_SESSION.add(symbol)
    try:
        if extended:
            limit = round(ref_px, 2)
            order_id, avg, filled = await _extended_buy(symbol, qty, limit, trail)
            if filled <= 0:
                return False
            fill_price = avg or limit
        else:
            order_id, fill_price = await place_bracket_buy(
                symbol, qty, take_profit_pct=0.15, stop_loss_pct=0.04,
                use_trailing=True, trailing_stop_pct=trail,
            )
            if not order_id:
                logger.info("NEWS BUY %s — regular-hours bracket buy not placed", symbol)
                return False
            filled = float(qty)
            o = await _get_order_by_id(order_id)
            if o and float(o.get("filled_qty") or 0) > 0:
                filled = float(o.get("filled_qty"))
            fill_price = fill_price or snap["price"]
    finally:
        _PENDING_BUYS.discard(symbol)

    ledger = load_ledger()
    add_entry(
        ledger,
        symbol=symbol,
        side="long",
        qty=filled,
        entry_price=fill_price,
        order_id=order_id,
        strategy="newsbuy",
        metadata={
            "source": "newsbuy",
            "tier": verdict["tier"],
            "news_reason": verdict["reason"],
            "news_conf": verdict.get("conf", 0.0),
            "headline": headline,
            "article_id": str(article.get("id", "")),
            "article_url": str(article.get("url", ""))[:300],
            "chg_vs_prior_close_pct": round(snap["chg_pct"], 2),
            "extended_hours": extended,
            "use_trailing": True,
            "trailing_stop_pct": trail,
            "size_multiplier": mult,
            "social_hot": bool(buzz and buzz.get("hot")),
            "social_reason": (buzz or {}).get("reason", ""),
            "crowd_bullish_pct": (buzz or {}).get("bullish_pct"),
        },
    )
    save_ledger(ledger)
    logger.warning(
        "NEWS BUY %s qty=%d @%.2f (%s, %+.1f%% vs prior close, %s) | %s",
        symbol, int(filled), fill_price, tag, snap["chg_pct"], "extended" if extended else "regular", headline,
    )
    return True


# ── main loop ────────────────────────────────────────────────────────────────
_seen_ids: set[str] = set()
_last_poll: Optional[datetime] = None
_symbol_rejected: dict[str, datetime] = {}


def _window_open(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    last = int(_cfg_f("NEWS_BUY_LAST_MINUTE_ET", 19 * 60 + 30))
    return 4 * 60 <= minutes < last


def _candidates(article: dict[str, Any]) -> list[str]:
    raw = [s for s in (article.get("symbols") or []) if isinstance(s, str)]
    if not raw or len(raw) > int(_cfg_f("NEWS_BUY_MAX_TAGGED", 2)):
        return []  # untagged or a multi-name roundup
    syms = [s.upper() for s in raw if _VALID_SYM.match(s.upper())]
    if len(syms) != len(raw):
        return []  # crypto / odd tags mixed in
    if len(syms) == 1:
        return syms
    text = f"{article.get('headline', '')} {article.get('summary', '')}"
    return [s for s in syms if re.search(rf"\b{s}\b", text)]


async def _poll_once() -> None:
    global _last_poll
    now_utc = datetime.now(timezone.utc)
    max_age = timedelta(minutes=_cfg_f("NEWS_BUY_MAX_AGE_MINUTES", 20.0))
    start = now_utc - max_age
    if _last_poll is not None:
        start = max(start, _last_poll - timedelta(minutes=2))

    base = str(getattr(config, "ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
    articles: list[dict[str, Any]] = []
    token = None
    for _ in range(3):
        params = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "limit": 50,
            "sort": "desc",
            "include_content": "true",
        }
        if token:
            params["page_token"] = token
        resp = await _request("GET", f"{base}/v1beta1/news", params=params)
        if resp.status_code != 200:
            logger.warning("news_buy: news fetch %s %s", resp.status_code, resp.text[:200])
            if not articles:
                return  # nothing fetched: keep the old window so the next poll retries it
            break
        data = resp.json()
        articles.extend(a for a in (data.get("news") or []) if isinstance(a, dict))
        token = data.get("next_page_token")
        if not token:
            break

    _last_poll = now_utc
    fresh = []
    for a in articles:
        aid = str(a.get("id", ""))
        if not aid or aid in _seen_ids:
            continue
        _seen_ids.add(aid)
        created = _parse_ts(a.get("created_at"))
        if created is None or now_utc - created > max_age:
            continue
        if _candidates(a):
            fresh.append(a)
    if len(_seen_ids) > 5000:
        _seen_ids.clear()
    if not fresh:
        return

    finbert_budget = int(_cfg_f("NEWS_BUY_MAX_FINBERT_PER_POLL", 25))
    for a in sorted(fresh, key=lambda x: str(x.get("created_at", ""))):
        if finbert_budget <= 0:
            break
        finbert_budget -= 1
        verdict = await classify_buy(a)
        if not verdict:
            continue
        for symbol in _candidates(a):
            try:
                await _consider(a, symbol, verdict)
            except Exception:
                logger.exception("news_buy: failed evaluating %s", symbol)


async def _consider(article: dict[str, Any], symbol: str, verdict: dict[str, Any]) -> None:
    head = str(article.get("headline") or "")[:120]

    def skip(why: str) -> None:
        logger.info("NEWS BUY candidate %s skipped — %s | tier %s %s | %s", symbol, why, verdict["tier"], verdict["reason"], head)

    if _blocklisted(symbol):
        return skip("blocklisted")
    last_rej = _symbol_rejected.get(symbol)
    if last_rej and datetime.now(timezone.utc) - last_rej < timedelta(minutes=10):
        return skip("recently rejected")
    if await _daily_loss_halt():
        return skip("daily loss halt")
    async with _buy_lock:
        ledger = load_ledger()
        why = _ledger_checks(ledger, symbol, verdict["tier"])
        if why:
            return skip(why)
        positions = await _get_open_positions()
        if any(str(p.get("symbol", "")).upper() == symbol for p in positions):
            return skip("already held")
        if len(positions) >= int(_cfg_f("MAX_POSITIONS", 25)):
            return skip("position cap")
        if not await _asset_ok(symbol):
            return skip("asset not tradable / OTC")
        snap = await _snapshot(symbol)
        if not snap:
            _symbol_rejected[symbol] = datetime.now(timezone.utc)
            return skip("no snapshot")
        extended = not _is_regular_market_hours()
        if snap["trade_age_s"] > _cfg_f("NEWS_BUY_MAX_TRADE_AGE_SECONDS", 900.0):
            return skip(f"last trade {snap['trade_age_s']:.0f}s old")
        if snap["price"] < _cfg_f("MIN_PRICE", 4.0):
            return skip(f"price {snap['price']:.2f} below floor")
        if snap["prev_dollar_vol"] < _cfg_f("NEWS_BUY_MIN_PREV_DOLLAR_VOL", 2_000_000.0):
            return skip(f"thin (prior $vol {snap['prev_dollar_vol']:,.0f})")
        if snap["chg_pct"] > _cfg_f("NEWS_BUY_MAX_CHASE_PCT", 8.0):
            return skip(f"already {snap['chg_pct']:+.1f}% vs prior close (chase cap)")
        buzz: Optional[dict[str, Any]] = None
        if _cfg_b("NEWS_BUY_SOCIAL_ENABLED", True):
            from backend.social_buzz import buzz_snapshot

            buzz = await buzz_snapshot(symbol)
            if buzz.get("bearish"):
                return skip(f"StockTwits crowd bearish ({buzz.get('bullish_pct', 0):.0%} bullish of {buzz.get('tagged')} tagged)")
        if verdict["tier"] == "B":
            need = _cfg_f("NEWS_BUY_WIDE_MIN_MOVE_PCT", 1.0)
            if snap["chg_pct"] < need and not (buzz and buzz.get("hot") and snap["chg_pct"] >= 0.0):
                return skip(f"wide-net needs price confirmation (+{need:g}%) or social + flat/up, only {snap['chg_pct']:+.1f}%")
        if verdict["tier"] == "C":
            if not (buzz and buzz.get("hot")):
                return skip("social-led: no longer hot")
            need = _cfg_f("NEWS_BUY_SOCIAL_MIN_MOVE_PCT", 2.0)
            if snap["chg_pct"] < need:
                return skip(f"social-led needs price confirmation (+{need:g}%), only {snap['chg_pct']:+.1f}%")
        sp = snap["spread_pct"]
        if sp is None:
            if extended:
                return skip("no quote outside regular hours")
        elif sp > _cfg_f("NEWS_BUY_MAX_SPREAD_PCT", 1.5):
            return skip(f"spread {sp:.2f}%")
        neg = await _recent_negative(symbol)
        if neg:
            return skip(neg)
        await _execute_buy(article, symbol, verdict, snap, buzz)


_social_checked: dict[str, float] = {}


async def _poll_social_once() -> None:
    """Social-led discovery (tier C): hot Reddit / StockTwits-trending tickers that
    also have a recent positive Alpaca article. Buzz alone never buys."""
    from backend.social_buzz import hot_symbols

    hot = await hot_symbols(int(_cfg_f("NEWS_BUY_SOCIAL_MAX_CANDIDATES", 25)))
    if not hot:
        return
    now = time.time()
    hours = _cfg_f("NEWS_BUY_SOCIAL_NEWS_HOURS", 3.0)
    min_conf = _cfg_f("NEWS_BUY_SOCIAL_MIN_CONF", 0.85)
    for symbol, info in hot:
        if not _VALID_SYM.match(symbol) or _blocklisted(symbol):
            continue
        if now - _social_checked.get(symbol, 0.0) < 3600.0:
            continue
        _social_checked[symbol] = now
        await asyncio.sleep(1.0)  # be gentle: the main scan shares this Alpaca data rate limit
        try:
            arts = await _fetch_news([symbol], datetime.now(timezone.utc) - timedelta(hours=hours))
        except Exception:
            continue
        arts = [a for a in arts if symbol in _candidates(a)]
        arts.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        for a in arts[:2]:
            verdict = await classify_buy(a, wide_conf=min_conf)
            if not verdict:
                continue
            verdict = {**verdict, "tier": "C", "reason": f"social-led ({info.get('mentions', 0)} reddit mentions x{info.get('ratio', 0)}) + {verdict['reason']}"}
            await _consider(a, symbol, verdict)
            break
    if len(_social_checked) > 2000:
        _social_checked.clear()


async def monitor_news_buys() -> None:
    """Background task: poll the whole news stream and buy strong positive catalysts."""
    global _last_poll
    if not _cfg_b("NEWS_BUY_ENABLED", True):
        logger.info("news_buy disabled (NEWS_BUY_ENABLED=false)")
        return
    interval = max(20.0, _cfg_f("NEWS_BUY_INTERVAL_SECONDS", 60.0))
    logger.warning(
        "news_buy started — interval %.0fs, %s, wide-net=%s, social=%s/led=%s, max %d/day",
        interval,
        "DRY RUN" if _cfg_b("NEWS_BUY_DRY_RUN", False) else "LIVE",
        _cfg_b("NEWS_BUY_WIDE_ENABLED", True),
        _cfg_b("NEWS_BUY_SOCIAL_ENABLED", True),
        _cfg_b("NEWS_BUY_SOCIAL_LED_ENABLED", True),
        int(_cfg_f("NEWS_BUY_MAX_PER_DAY", 4)),
    )
    last_social = 0.0
    social_every = max(60.0, _cfg_f("NEWS_BUY_SOCIAL_INTERVAL_SECONDS", 300.0))
    while True:
        try:
            now = datetime.now(_ET)
            if _window_open(now) and await _is_trading_day(now):
                await _poll_once()
                if _cfg_b("NEWS_BUY_SOCIAL_LED_ENABLED", True) and time.time() - last_social >= social_every:
                    last_social = time.time()
                    await _poll_social_once()
            else:
                _last_poll = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("news_buy: poll failed")
        await asyncio.sleep(interval)
