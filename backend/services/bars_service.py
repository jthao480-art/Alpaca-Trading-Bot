import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from .auth import alpaca_request_async

logger = logging.getLogger(__name__)


class Bars_Service:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bars_cache = {}
        self._news_cache = {}
        self._cache_ttl_seconds = 45

    def _cache_get(self, cache: dict, key):
        item = cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self._cache_ttl_seconds:
            cache.pop(key, None)
            return None
        return value

    def _cache_set(self, cache: dict, key, value):
        cache[key] = (time.time(), value)

    async def _get_with_retry(self, url, *, params=None, cache=None, cache_key=None):
        if cache is not None and cache_key is not None:
            cached = self._cache_get(cache, cache_key)
            if cached is not None:
                return cached

        delay = 1.5
        last_exc = None

        for attempt in range(1, 6):
            try:
                resp = await alpaca_request_async("GET", url, params=params, use_data_api=True)

                if resp.status_code == 429:
                    logger.warning(
                        "429 on %s (attempt %s/5); backing off %.1fs",
                        url,
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

    async def get_bars(self, symbol, start, end, timeframe="5Min", limit=60, feed="iex"):
        path = f"/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": limit,
            "feed": feed,
            "sort": "asc",
        }
        key = ("bars", symbol, start, end, timeframe, limit, feed)
        return await self._get_with_retry(path, params=params, cache=self._bars_cache, cache_key=key)

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


_service = Bars_Service()


def _default_window(days: int = 7) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


async def get_bars(symbol, timeframe="5Min", limit=60, feed="iex"):
    start, end = _default_window(7)
    payload = await _service.get_bars(symbol, start, end, timeframe=timeframe, limit=limit, feed=feed)
    return payload.get("bars", []) if isinstance(payload, dict) else []


async def get_latest_bar(symbol, timeframe="5Min", limit=1, feed="iex"):
    bars = await get_bars(symbol, timeframe=timeframe, limit=limit, feed=feed)
    return bars[-1] if bars else {}


async def get_latest_quote(*args, **kwargs):
    raise NotImplementedError("get_latest_quote is not implemented in BarsService yet")


async def get_latest_trade(*args, **kwargs):
    raise NotImplementedError("get_latest_trade is not implemented in BarsService yet")


async def get_recent_bars(symbol, timeframe="5Min", limit=60, feed="iex"):
    return await get_bars(symbol, timeframe=timeframe, limit=limit, feed=feed)