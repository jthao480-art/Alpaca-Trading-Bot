import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import alpaca_request_async

logger = logging.getLogger(__name__)


class NewsService:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._news_cache = {}
        self._sentiment_cache = {}
        self._cache_ttl_seconds = 45
        self._sentiment_ttl_seconds = 300
        self._nlp = None

    def _cache_get(self, cache: dict, key):
        item = cache.get(key)
        if not item:
            return None
        ts, value = item
        ttl = self._cache_ttl_seconds if cache is self._news_cache else self._sentiment_ttl_seconds
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None
        return value

    def _cache_set(self, cache: dict, key, value):
        cache[key] = (time.time(), value)

    def _get_nlp(self):
        if self._nlp is not None:
            return self._nlp
        try:
            from transformers import pipeline  # pyright: ignore[reportMissingImports]
            self._nlp = pipeline("sentiment-analysis", model="ProsusAI/finbert")
        except Exception as exc:
            logger.warning("FinBERT unavailable, using fallback scorer: %s", exc)
            self._nlp = None
        return self._nlp

    def _score_headlines_fallback(self, headlines: list[str]) -> dict[str, Any]:
        if not headlines:
            return {"score": 0.5, "label": "neutral", "confidence": 0.0, "details": []}

        text = " ".join(headlines).lower()
        positive_words = [
            "beat", "beats", "bullish", "growth", "surge", "rally",
            "strong", "upgrade", "positive", "profit", "record",
        ]
        negative_words = [
            "miss", "misses", "bearish", "drop", "fall", "weak",
            "downgrade", "negative", "loss", "lawsuit", "investigation",
        ]

        pos = sum(text.count(word) for word in positive_words)
        neg = sum(text.count(word) for word in negative_words)

        score = 0.5 + min(pos * 0.06, 0.30) - min(neg * 0.06, 0.30)
        score = max(0.0, min(1.0, round(score, 4)))

        if score >= 0.60:
            label = "positive"
        elif score <= 0.40:
            label = "negative"
        else:
            label = "neutral"

        return {"score": score, "label": label, "confidence": 0.0, "details": []}

    async def _score_headlines_finbert(self, headlines: list[str], batch_size: int = 16) -> dict[str, Any]:
        headlines = [h for h in headlines if isinstance(h, str) and h.strip()]
        if not headlines:
            return {"score": 0.5, "label": "neutral", "confidence": 0.0, "details": []}

        cache_key = ("finbert", tuple(headlines[:50]), batch_size)
        cached = self._cache_get(self._sentiment_cache, cache_key)
        if cached is not None:
            return cached

        nlp = self._get_nlp()
        if nlp is None:
            out = self._score_headlines_fallback(headlines)
            self._cache_set(self._sentiment_cache, cache_key, out)
            return out

        loop = asyncio.get_running_loop()

        def run_batches():
            all_results = []
            for i in range(0, min(len(headlines), 50), batch_size):
                chunk = headlines[i : i + batch_size]
                res = nlp(chunk, truncation=True)
                if isinstance(res, dict):
                    res = [res]
                all_results.extend(res)
            return all_results

        results = await loop.run_in_executor(None, run_batches)

        details: list[dict[str, Any]] = []
        pos_sum = 0.0
        neg_sum = 0.0
        neu_sum = 0.0

        for text, item in zip(headlines[:50], results):
            label = str(item.get("label", "neutral")).lower()
            conf = float(item.get("score", 0.0) or 0.0)

            if label == "positive":
                pos_sum += conf
            elif label == "negative":
                neg_sum += conf
            else:
                neu_sum += conf

            details.append(
                {
                    "text": text,
                    "label": label,
                    "score": round(conf, 6),
                }
            )

        n = max(len(results), 1)
        pos_avg = pos_sum / n
        neg_avg = neg_sum / n
        neu_avg = neu_sum / n

        signed = 0.5 + (pos_avg - neg_avg)
        signed = max(0.0, min(1.0, signed))

        if pos_avg > neg_avg and pos_avg >= neu_avg:
            label = "positive"
            confidence = pos_avg
        elif neg_avg > pos_avg and neg_avg >= neu_avg:
            label = "negative"
            confidence = neg_avg
        else:
            label = "neutral"
            confidence = neu_avg

        out = {
            "score": round(signed, 4),
            "label": label,
            "confidence": round(confidence, 4),
            "details": details,
        }
        self._cache_set(self._sentiment_cache, cache_key, out)
        return out

    async def _get_with_retry(self, path, *, params=None, cache=None, cache_key=None):
        if cache is not None and cache_key is not None:
            cached = self._cache_get(cache, cache_key)
            if cached is not None:
                return cached

        delay = 1.5
        last_exc = None

        for attempt in range(1, 6):
            try:
                resp = await alpaca_request_async("GET", path, params=params, use_data_api=True)

                if resp.status_code == 429:
                    logger.warning(
                        "429 on %s (attempt %s/5); backing off %.1fs",
                        path,
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue

                resp.raise_for_status()
                data = resp.json()

                if cache is not None and cache_key is not None:
                    self._cache_set(cache, cache_key, data)

                return data

            except Exception as exc:
                last_exc = exc
                if attempt == 5:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

        raise last_exc

    async def get_news(self, symbol, start, end, limit=50):
        path = "/v1beta1/news"
        params = {
            "symbols": symbol,
            "start": start,
            "end": end,
            "limit": limit,
            "sort": "desc",
        }
        key = ("news", symbol, start, end, limit)
        return await self._get_with_retry(path, params=params, cache=self._news_cache, cache_key=key)


_service = NewsService()


def _default_window(days: int = 3) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _extract_articles(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("news") or payload.get("data") or payload.get("articles") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _extract_headlines(payload: Any) -> list[str]:
    headlines: list[str] = []
    for item in _extract_articles(payload):
        headline = item.get("headline") or item.get("title")
        summary = item.get("summary") or item.get("description") or ""
        text = headline or summary
        if text:
            headlines.append(str(text))
    return headlines

def _default_window_hours(hours: int = 2) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return start.isoformat(), end.isoformat()

async def get_news_sentiment(symbol, start=None, end=None, limit=50, batch_size=16):
    if start is None or end is None:
        start, end = _default_window(3)
    try:
        # Fetch 3-day window for sentiment scoring
        payload = await _service.get_news(symbol, start, end, limit=limit)
        headlines = _extract_headlines(payload)
        finbert = await _service._score_headlines_finbert(headlines, batch_size=batch_size)

        # Fetch 2-hour window for velocity scoring
        recent_start, recent_end = _default_window_hours(2)
        try:
            recent_payload = await _service.get_news(symbol, recent_start, recent_end, limit=10)
            recent_headlines = _extract_headlines(recent_payload)
            recent_count = len(recent_headlines)
        except Exception:
            recent_count = 0

        # Velocity = recent articles per hour vs 3-day baseline per hour
        baseline_per_hour = len(headlines) / 72  # 3 days = 72 hours
        recent_per_hour = recent_count / 2        # 2-hour window
        velocity_ratio = (recent_per_hour / baseline_per_hour) if baseline_per_hour > 0 else 1.0
        news_spike = velocity_ratio >= 2.0        # 2x normal rate = spike

        # Velocity score: 0.0-1.0 (1x=0.5, 2x+=1.0, 0x=0.0)
        velocity_score = min(1.0, max(0.0, velocity_ratio / 4.0))

        return {
            "score": finbert["score"],
            "label": finbert["label"],
            "confidence": finbert["confidence"],
            "headlines": headlines[:10],
            "count": len(headlines),
            "recent_count": recent_count,
            "velocity_ratio": round(velocity_ratio, 2),
            "velocity_score": round(velocity_score, 3),
            "news_spike": news_spike,
            "raw": payload,
            "error": None,
            "details": finbert.get("details", []),
        }
    except Exception as exc:
        logger.exception("get_news_sentiment failed for %s", symbol)
        return {
            "score": 0.5,
            "label": "neutral",
            "confidence": 0.0,
            "headlines": [],
            "count": 0,
            "recent_count": 0,
            "velocity_ratio": 1.0,
            "velocity_score": 0.25,
            "news_spike": False,
            "raw": None,
            "error": str(exc),
            "details": [],
        }