"""
backend/services/stocktwits_service.py

Multi-source social sentiment using:
1. StockTwits  - per-symbol stream + bullish/bearish ratio (curl_cffi bypasses Cloudflare)
2. ApeWisdom   - Reddit mention count + velocity (no API key needed)

All HTTP calls are non-blocking via run_in_executor.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import requests as std_requests
from curl_cffi import requests as cf_requests  # type: ignore
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # type: ignore

logger = logging.getLogger(__name__)

STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
APEWISDOM_BASE  = "https://apewisdom.io/api/v1.0"

_analyzer = SentimentIntensityAnalyzer()

# Cache — TTL 5 minutes per symbol
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300

# Rolling baseline for spike detection
_volume_baseline: dict[str, list[float]] = {}
_MAX_BASELINE_SAMPLES = 20


def _cache_get(symbol: str) -> dict | None:
    entry = _cache.get(symbol)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(symbol, None)
        return None
    return data


def _cache_set(symbol: str, data: dict) -> None:
    _cache[symbol] = (time.time(), data)


def _update_baseline(symbol: str, volume: float) -> float:
    samples = _volume_baseline.setdefault(symbol, [])
    samples.append(volume)
    if len(samples) > _MAX_BASELINE_SAMPLES:
        samples.pop(0)
    return sum(samples) / len(samples) if samples else volume


# ── Sync fetch functions (called via run_in_executor) ────────────────────────

def _fetch_stocktwits(ticker: str) -> dict:
    """Fetch StockTwits using curl_cffi to bypass Cloudflare. Sync."""
    try:
        r = cf_requests.get(
            f"{STOCKTWITS_BASE}/streams/symbol/{ticker.upper()}.json",
            impersonate="chrome",
            timeout=8,
        )
        if r.status_code != 200:
            logger.debug("StockTwits returned %s for %s", r.status_code, ticker)
            return {}
        return r.json()
    except Exception as e:
        logger.debug("StockTwits fetch failed for %s: %s", ticker, e)
        return {}


def _fetch_apewisdom(ticker: str) -> dict:
    """Get Reddit mention data via ApeWisdom. Sync."""
    try:
        r = std_requests.get(
            f"{APEWISDOM_BASE}/filter/all-stocks",
            headers={"User-Agent": "TradingBot/1.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return {}
        for item in r.json().get("results", []):
            if item.get("ticker", "").upper() == ticker.upper():
                return item
        return {}
    except Exception as e:
        logger.debug("ApeWisdom fetch failed for %s: %s", ticker, e)
        return {}


# ── Async wrappers ────────────────────────────────────────────────────────────

async def _fetch_stocktwits_async(ticker: str) -> dict:
    """Non-blocking StockTwits fetch via thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_stocktwits, ticker)


async def _fetch_apewisdom_async(ticker: str) -> dict:
    """Non-blocking ApeWisdom fetch via thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_apewisdom, ticker)


# ── Main public functions ─────────────────────────────────────────────────────

def _parse_sentiment(st_data: dict, ape_data: dict, symbol: str) -> dict[str, Any]:
    """Parse raw fetch results into unified sentiment dict."""
    messages = st_data.get("messages", [])

    bullish = 0
    bearish = 0
    sentiments = []

    for m in messages:
        entities = m.get("entities", {}) or {}
        sentiment = entities.get("sentiment", {}) or {}
        basic = sentiment.get("basic", "").lower() if sentiment else ""
        if basic == "bullish":
            bullish += 1
        elif basic == "bearish":
            bearish += 1

        body = m.get("body", "")
        if body:
            score = _analyzer.polarity_scores(body)
            sentiments.append(score["compound"])

    st_count = len(messages)
    total_tagged = bullish + bearish
    bullish_pct = bullish / total_tagged if total_tagged > 0 else 0.5
    bearish_pct = bearish / total_tagged if total_tagged > 0 else 0.5
    avg_vader = sum(sentiments) / len(sentiments) if sentiments else 0.0

    # ApeWisdom
    reddit_mentions = int(ape_data.get("mentions", 0) or 0)
    mentions_24h_ago = int(ape_data.get("mentions_24h_ago", 0) or 0)
    reddit_rank = ape_data.get("rank")
    mention_velocity = (
        (reddit_mentions - mentions_24h_ago) / mentions_24h_ago
        if mentions_24h_ago > 0
        else 0.0
    )

    # Combined sentiment score 0.0-1.0
    if total_tagged > 0:
        sentiment_score = bullish_pct
    else:
        sentiment_score = 0.5 + (avg_vader * 0.5)
    sentiment_score = max(0.0, min(1.0, sentiment_score))

    # Spike detection
    total_volume = st_count + reddit_mentions
    avg_baseline = _update_baseline(symbol, total_volume)
    spike_ratio = total_volume / avg_baseline if avg_baseline > 0 else 1.0
    spike = spike_ratio >= 2.0
    velocity_score = min(1.0, max(0.0, (spike_ratio - 1.0) / 3.0))

    return {
        "symbol": symbol.upper(),
        "message_count": st_count,
        "bullish": bullish,
        "bearish": bearish,
        "bullish_pct": round(bullish_pct, 3),
        "bearish_pct": round(bearish_pct, 3),
        "sentiment_score": round(sentiment_score, 3),
        "avg_vader": round(avg_vader, 3),
        "spike": spike,
        "spike_ratio": round(spike_ratio, 2),
        "velocity_score": round(velocity_score, 3),
        "reddit_mentions": reddit_mentions,
        "mention_velocity": round(mention_velocity, 2),
        "reddit_rank": reddit_rank,
        "error": None,
    }


async def get_stocktwits_sentiment(symbol: str) -> dict[str, Any]:
    """
    Fetch unified social sentiment from StockTwits + Reddit (ApeWisdom).
    Async and non-blocking — both sources fetched concurrently.
    """
    cached = _cache_get(symbol)
    if cached:
        return cached

    ticker = symbol.upper()

    # Fetch both sources concurrently
    results = await asyncio.gather(
        _fetch_stocktwits_async(ticker),
        _fetch_apewisdom_async(ticker),
        return_exceptions=True,
    )

    st_data = results[0] if not isinstance(results[0], Exception) else {}
    ape_data = results[1] if not isinstance(results[1], Exception) else {}

    result = _parse_sentiment(st_data, ape_data, symbol)
    _cache_set(symbol, result)
    return result


async def get_social_spike_score(symbol: str) -> float:
    """
    Returns 0.0-1.0 score for social spike intensity.
    Async and non-blocking.
    Weights: 0.50 sentiment, 0.30 spike magnitude, 0.20 velocity
    """
    try:
        data = await get_stocktwits_sentiment(symbol)

        if data.get("error") and data["message_count"] == 0:
            return 0.5

        sentiment = float(data.get("sentiment_score", 0.5))
        spike_ratio = float(data.get("spike_ratio", 1.0))
        velocity = float(data.get("velocity_score", 0.0))

        reddit_mentions = int(data.get("reddit_mentions", 0))
        mention_velocity = float(data.get("mention_velocity", 0.0))
        reddit_boost = min(0.10, reddit_mentions / 1000) if reddit_mentions > 50 else 0.0
        if mention_velocity > 0.5:
            reddit_boost += 0.05

        spike_score = min(1.0, max(0.0, (spike_ratio - 1.0) / 3.0))

        score = (
            0.50 * sentiment
            + 0.30 * spike_score
            + 0.20 * velocity
            + reddit_boost
        )

        return round(min(1.0, max(0.0, score)), 3)

    except Exception:
        logger.exception("get_social_spike_score failed for %s", symbol)
        return 0.5