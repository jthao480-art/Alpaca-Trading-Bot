"""
Social "buzz" layer for news_buy.py -- a second source next to Alpaca news.

Sources (no API keys needed; both already used by agents/social_agent.py):
  * ApeWisdom  -- Reddit mention counts + 24h-ago comparison for the top ~200
                  tickers (r/wallstreetbets, r/stocks, r/investing, ...).
  * StockTwits -- the platform's trending-symbols list, plus per-symbol
                  bullish/bearish tagging via services.stocktwits_service.

What it answers
  hot      : is this ticker suddenly being talked about? (Reddit mentions >=
             SOCIAL_MIN_MENTIONS AND (>= SOCIAL_MIN_RATIO x yesterday's mentions OR
             climbed >= SOCIAL_MIN_RANK_JUMP ranks), or on StockTwits trending)
  bearish  : is the tagged StockTwits crowd clearly bearish? (bullish share <=
             SOCIAL_BEARISH_BULLISH_PCT with >= SOCIAL_BEARISH_MIN_TAGGED tagged
             messages) -- used as a veto on a positive-news buy
  hot_symbols(): the discovery list for social-led (Tier C) candidates.

X/Twitter is deliberately not here: its API is paid-only. Everything fails soft --
if a source is down the function returns "not hot / not bearish" and the news
logic carries on without it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend import config

logger = logging.getLogger(__name__)

_APE_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks"
_ST_TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
_CACHE_TTL = 300.0

_ape_cache: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})
_trend_cache: tuple[float, list[str]] = (0.0, [])


def _cfg_f(name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except Exception:
        return default


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _fetch_ape_sync() -> dict[str, dict[str, Any]]:
    import requests

    out: dict[str, dict[str, Any]] = {}
    for page in (1, 2):
        try:
            r = requests.get(f"{_APE_URL}/page/{page}", headers={"User-Agent": "TradingBot/1.0"}, timeout=8)
            if r.status_code != 200:
                break
            for item in r.json().get("results", []):
                t = str(item.get("ticker", "")).upper()
                if t:
                    out[t] = item
        except Exception as exc:
            logger.debug("social_buzz: ApeWisdom page %s failed: %s", page, exc)
            break
    return out


def _fetch_trending_sync() -> list[str]:
    try:
        from curl_cffi import requests as cf_requests  # type: ignore

        r = cf_requests.get(_ST_TRENDING_URL, impersonate="chrome", timeout=8)
        if r.status_code != 200:
            return []
        return [str(s.get("symbol", "")).upper() for s in r.json().get("symbols", []) if s.get("symbol")]
    except Exception as exc:
        logger.debug("social_buzz: StockTwits trending failed: %s", exc)
        return []


async def apewisdom_table() -> dict[str, dict[str, Any]]:
    global _ape_cache
    ts, data = _ape_cache
    if data and time.time() - ts < _CACHE_TTL:
        return data
    loop = asyncio.get_running_loop()
    fresh = await loop.run_in_executor(None, _fetch_ape_sync)
    if fresh:
        _ape_cache = (time.time(), fresh)
        return fresh
    return data  # stale beats nothing


async def stocktwits_trending() -> list[str]:
    global _trend_cache
    ts, data = _trend_cache
    if data and time.time() - ts < _CACHE_TTL:
        return data
    loop = asyncio.get_running_loop()
    fresh = await loop.run_in_executor(None, _fetch_trending_sync)
    if fresh:
        _trend_cache = (time.time(), fresh)
        return fresh
    return data


def _ape_hot(item: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not item:
        return None
    mentions = _to_int(item.get("mentions"))
    prev = _to_int(item.get("mentions_24h_ago"))
    rank = _to_int(item.get("rank"))
    prev_rank = _to_int(item.get("rank_24h_ago"))
    if mentions < _cfg_f("NEWS_BUY_SOCIAL_MIN_MENTIONS", 20):
        return None
    ratio = mentions / max(prev, 1)
    jump = (prev_rank - rank) if prev_rank and rank else 0
    if ratio >= _cfg_f("NEWS_BUY_SOCIAL_MIN_RATIO", 2.0) or jump >= _cfg_f("NEWS_BUY_SOCIAL_MIN_RANK_JUMP", 20):
        return {"rank": rank, "mentions": mentions, "prev_mentions": prev, "ratio": round(ratio, 2), "rank_jump": jump}
    return None


async def hot_symbols(limit: int = 25) -> list[tuple[str, dict[str, Any]]]:
    """Discovery list: Reddit-hot and StockTwits-trending tickers, strongest first."""
    table, trending = await asyncio.gather(apewisdom_table(), stocktwits_trending())
    found: dict[str, dict[str, Any]] = {}
    for t, item in table.items():
        h = _ape_hot(item)
        if h:
            found[t] = {**h, "reddit": True, "trending": t in trending}
    for rank, t in enumerate(trending[:30], start=1):
        if t in found:
            continue
        found[t] = {"rank": 0, "mentions": 0, "prev_mentions": 0, "ratio": 0.0, "rank_jump": 0,
                    "reddit": False, "trending": True, "st_rank": rank}
    ordered = sorted(found.items(), key=lambda kv: (kv[1].get("trending", False) and kv[1].get("reddit", False), kv[1].get("ratio", 0.0)), reverse=True)
    return ordered[:limit]


async def buzz_snapshot(symbol: str) -> dict[str, Any]:
    """{"hot": bool, "bearish": bool, ...} -- never raises."""
    symbol = symbol.upper()
    snap: dict[str, Any] = {"hot": False, "bearish": False, "reason": "", "bullish_pct": None, "tagged": 0}
    try:
        table, trending = await asyncio.gather(apewisdom_table(), stocktwits_trending())
        h = _ape_hot(table.get(symbol))
        on_trend = symbol in trending[:30]
        if h:
            snap.update(h)
            snap["hot"] = True
            snap["reason"] = f"reddit x{h['ratio']:.1f} ({h['mentions']} mentions, rank {h['rank']})"
        if on_trend:
            snap["hot"] = True
            snap["trending"] = True
            snap["reason"] = (snap["reason"] + " + " if snap["reason"] else "") + "stocktwits trending"
    except Exception:
        logger.exception("social_buzz: hot check failed for %s", symbol)
    try:
        from backend.services.stocktwits_service import get_stocktwits_sentiment

        s = await get_stocktwits_sentiment(symbol)
        tagged = int(s.get("bullish", 0)) + int(s.get("bearish", 0))
        snap["tagged"] = tagged
        if tagged >= 1:
            snap["bullish_pct"] = float(s.get("bullish_pct", 0.5))
        if tagged >= _cfg_f("NEWS_BUY_SOCIAL_BEARISH_MIN_TAGGED", 8) and float(s.get("bullish_pct", 0.5)) <= _cfg_f(
            "NEWS_BUY_SOCIAL_BEARISH_BULLISH_PCT", 0.35
        ):
            snap["bearish"] = True
    except Exception:
        logger.debug("social_buzz: StockTwits sentiment failed for %s", symbol)
    return snap
