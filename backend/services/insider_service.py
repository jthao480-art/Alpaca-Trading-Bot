"""
services/insider_service.py - Fetch insider filings from SEC EDGAR.
Uses SEC submissions API with ticker-to-CIK lookup. Free, no API key needed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
import asyncio

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "TradingBot research@trading.com"}

# Caches
_cik_cache: dict[str, str] = {}
_filing_cache: dict[str, tuple[float, list]] = {}
_ticker_map: dict[str, int] = {}  # ticker -> cik_str
_ticker_map_loaded = False
_CACHE_TTL = 1800  # 30 minutes


def _load_ticker_map() -> None:
    global _ticker_map, _ticker_map_loaded
    if _ticker_map_loaded:
        return
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            for val in data.values():
                ticker = str(val.get("ticker", "")).upper()
                cik = val.get("cik_str", 0)
                if ticker and cik:
                    _ticker_map[ticker] = int(cik)
            _ticker_map_loaded = True
            logger.info("Loaded %d tickers from SEC", len(_ticker_map))
    except Exception as e:
        logger.warning("Failed to load SEC ticker map: %s", e)


def _get_cik(symbol: str) -> str | None:
    _load_ticker_map()
    cik = _ticker_map.get(symbol.upper())
    if cik:
        return str(cik).zfill(10)
    return None


def _fetch_form4s(symbol: str, days_back: int = 30) -> list[dict]:
    cik = _get_cik(symbol)
    if not cik:
        logger.debug("No CIK found for %s", symbol)
        return []

    try:
        r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return []

        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        reporters = recent.get("primaryDocument", [])

        cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        filings = []
        for form, date, accession in zip(forms, dates, accessions):
            if form != "4":
                continue
            if date < cutoff:
                continue
            filings.append({
                "form_type": "4",
                "file_date": date,
                "entity_name": data.get("name", symbol),
                "accession": accession,
                "symbol": symbol,
            })

        return filings

    except Exception as e:
        logger.debug("SEC submissions fetch failed for %s: %s", symbol, e)
        return []


async def _fetch_form4s_async(symbol: str, days_back: int = 30) -> list[dict]:
    """Non-blocking Form 4 fetch via thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_form4s, symbol, days_back)


async def get_insider_filings(
    symbol: str,
    limit: int = 20,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[dict]:
    cached = _filing_cache.get(symbol)
    if cached:
        ts, data = cached
        if time.time() - ts < _CACHE_TTL:
            return data[:limit]

    days_back = 30
    if start:
        days_back = max(1, (datetime.utcnow() - start).days)

    filings = await _fetch_form4s_async(symbol, days_back=days_back)
    _filing_cache[symbol] = (time.time(), filings)
    return filings[:limit]

async def get_insider_sentiment(symbol: str, limit: int = 20) -> dict:
    filings = await get_insider_filings(symbol, limit=limit)

    count = len(filings)
    headlines = []
    for f in filings:
        date = f.get("file_date", "")
        entity = f.get("entity_name", symbol)
        if date:
            headlines.append(f"{entity} Form 4 filed {date}")

    # Score by filing frequency — more recent Form 4s = more insider activity
    if count == 0:
        score = 0.5
    elif count <= 2:
        score = 0.58
    elif count <= 5:
        score = 0.65
    elif count <= 10:
        score = 0.72
    else:
        score = 0.78

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "count": count,
        "headlines": headlines[:10],
        "error": None,
    }