import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from .auth import alpaca_request_async

logger = logging.getLogger(__name__)


class Bars_Service:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bars_cache = {}
        self._news_cache = {}
        self._cache_ttl_seconds = 45
        # Keys include start/end timestamps that change on every call, so entries
        # are rarely read again and were never evicted -> unbounded memory growth.
        # Cap the cache and drop expired/oldest entries on write.
        self._cache_max_entries = 1000
        # Data-API rate control. Scans fire hundreds of bar requests at once; when
        # Alpaca answers 429 every task used to back off on its own, retry into the
        # same wall and finally give up ("All retries exhausted"). Now:
        #  - at most BARS_MAX_CONCURRENCY requests in flight,
        #  - one 429 pauses ALL bar requests until Retry-After / the backoff elapses,
        #  - optional hard pacing via BARS_MAX_PER_MIN (0 = off; e.g. 190 for the free plan).
        self._max_concurrency = max(1, int(os.getenv("BARS_MAX_CONCURRENCY", "8") or 8))
        _per_min = float(os.getenv("BARS_MAX_PER_MIN", "0") or 0)
        self._min_interval = (60.0 / _per_min) if _per_min > 0 else 0.0
        self._sem = None
        self._blocked_until = 0.0
        self._next_slot = 0.0

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
        now = time.time()
        cache[key] = (now, value)
        if len(cache) > self._cache_max_entries:
            self._cache_evict(cache, now)

    def _cache_evict(self, cache: dict, now: float) -> None:
        # 1) drop everything past its TTL
        ttl = self._cache_ttl_seconds
        for k in [k for k, (ts, _) in cache.items() if now - ts > ttl]:
            cache.pop(k, None)
        # 2) still over the cap (burst inside the TTL): drop oldest-inserted down
        #    to 90% so we don't re-run this on every single write.
        target = int(self._cache_max_entries * 0.9)
        while len(cache) > target:
            cache.pop(next(iter(cache)), None)

    def _get_semaphore(self) -> asyncio.Semaphore:
        # created lazily so it binds to the running loop (older Pythons bind at construction)
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrency)
        return self._sem

    async def _wait_for_slot(self) -> None:
        """Wait out any global 429 pause, then (optionally) take a paced request slot."""
        while True:
            remaining = self._blocked_until - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 5.0))
        if self._min_interval:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
            if slot > now:
                await asyncio.sleep(slot - now)

    def _note_rate_limit(self, resp, fallback_delay: float, url: str, attempt: int) -> float:
        wait = fallback_delay
        try:
            ra = resp.headers.get("Retry-After")
            if ra:
                wait = max(wait, float(ra))
        except Exception:
            pass
        until = time.monotonic() + wait
        if until > self._blocked_until + 0.5:
            # only a request that meaningfully extends the pause logs, so a burst of
            # simultaneous 429s produces one line instead of hundreds
            self._blocked_until = until
            logger.warning("429 on %s (attempt %s/5); pausing all bar requests %.1fs", url, attempt, wait)
        return wait

    async def _get_with_retry(self, url, *, params=None, cache=None, cache_key=None):
        if cache is not None and cache_key is not None:
            cached = self._cache_get(cache, cache_key)
            if cached is not None:
                return cached

        delay = 1.5
        last_exc = None

        for attempt in range(1, 6):
            try:
                async with self._get_semaphore():
                    # checked after acquiring the semaphore so requests that queued up
                    # before a 429 arrived still honour the pause
                    await self._wait_for_slot()
                    resp = await alpaca_request_async("GET", url, params=params, use_data_api=True)

                if resp.status_code == 429:
                    self._note_rate_limit(resp, delay, url, attempt)
                    delay = min(delay * 2, 30.0)
                    last_exc = RuntimeError(f"429 rate limited: {url}")
                    continue

                resp.raise_for_status()
                data = resp.json()

                if cache is not None and cache_key is not None:
                    self._cache_set(cache, cache_key, data)

                return data

            except httpx.HTTPStatusError as exc:
                # 4xx (other than 408/429) will not succeed on retry: fail fast
                code = exc.response.status_code if exc.response is not None else 0
                if 400 <= code < 500 and code not in (408, 429):
                    raise
                last_exc = exc
                if attempt == 5:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            except Exception as exc:
                last_exc = exc
                if attempt == 5:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"All retries exhausted for {url}")

    async def get_bars(self, symbol, start, end, timeframe="5Min", limit=60, feed="iex"):
        path = f"/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": limit,
            "feed": feed,
            # Newest bars first: with sort=asc + start=14 days ago + a small limit, Alpaca
            # returns the OLDEST `limit` bars of the window (stale data for every agent,
            # the SPY trend filter and get_latest_price). The module-level get_bars()
            # below reverses the result so callers still see oldest -> newest.
            "sort": "desc",
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
    start, end = _default_window(14)
    payload = await _service.get_bars(symbol, start, end, timeframe=timeframe, limit=limit, feed=feed)
    bars = (payload.get("bars") or []) if isinstance(payload, dict) else []
    # requested newest-first (see Bars_Service.get_bars); return oldest -> newest, latest bar last
    return list(reversed(bars))


async def get_latest_bar(symbol, timeframe="5Min", limit=1, feed="iex"):
    bars = await get_bars(symbol, timeframe=timeframe, limit=limit, feed=feed)
    return bars[-1] if bars else {}


async def get_latest_quote(*args, **kwargs):
    raise NotImplementedError("get_latest_quote is not implemented in BarsService yet")


async def get_latest_trade(*args, **kwargs):
    raise NotImplementedError("get_latest_trade is not implemented in BarsService yet")


async def get_recent_bars(symbol, timeframe="5Min", limit=60, feed="iex"):
    return await get_bars(symbol, timeframe=timeframe, limit=limit, feed=feed)

    # Module-level cycle cache — cleared at start of each scan cycle
_bars_cache: dict[str, list[dict]] = {}

async def get_bars_cached(symbol: str, timeframe: str, limit: int) -> list[dict]:
    """Fetch bars with cycle-level caching — reuses data across agents."""
    key = f"{symbol}_{timeframe}"
    if key in _bars_cache:
        bars = _bars_cache[key]
        if not bars:
            return []
        return bars[-limit:] if limit < len(bars) else bars
    bars = await get_bars(symbol, timeframe=timeframe, limit=max(40, limit))
    if not bars:
        bars = []
    _bars_cache[key] = bars
    return bars[-limit:] if limit < len(bars) else bars

def clear_bars_cache() -> None:
    _bars_cache.clear()