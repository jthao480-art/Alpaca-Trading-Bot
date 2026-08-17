from __future__ import annotations

"""
tradetiq_agent.py — Tradetiq curated signal agent.

Calls /api/signals/todays-signals-bot ONCE per day and caches the result.
Each symbol in the curated list is returned as a buy signal when analyzed.

Individual signal types can be toggled via .env / Railway Variables:
    USE_TRADETIQ_RIPPLE=true/false       (Ripple provisional, 5-day hold)
    USE_TRADETIQ_ARES=true/false         (Ares provisional, 21-day hold)
    USE_TRADETIQ_WAVE=true/false         (Wave provisional, 5-day hold)
    USE_TRADETIQ_SMARTTIQ=true/false     (SmartTiq EOD, 21-day hold)
    USE_TRADETIQ_NEXUS=true/false        (Nexus EOD, 35-day hold)

Master switch:
    USE_TRADETIQ_AGENT=true/false

Hold windows (match Tradetiq validated research):
    ripple_provisional  → 5 trading days
    wave_provisional    → 5 trading days
    ares_provisional    → 21 trading days
    smarttiq            → 21 trading days
    nexus               → 35 trading days
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from backend.agents.base import BaseAgent
from backend import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_HOLD_DAYS: dict[str, int] = {
    "ripple_provisional": 5,
    "wave_provisional": 5,
    "ares_provisional": 21,
    "smarttiq": 21,
    "nexus": 35,
}

_GAIN_THRESHOLD: dict[str, float] = {
    "ripple_provisional": 0.15,
    "wave_provisional": 0.15,
    "ares_provisional": 0.30,
    "smarttiq": 0.30,
    "nexus": 0.40,
}

_CACHE: dict[str, Any] = {
    "date": None,
    "data": None,
    "symbol_map": {},
    "ts": 0.0,
}
_CACHE_LOCK = asyncio.Lock()


def _get_enabled_signal_types() -> set[str]:
    enabled = set()
    if getattr(config, "USE_TRADETIQ_RIPPLE", True):
        enabled.add("ripple_provisional")
    if getattr(config, "USE_TRADETIQ_ARES", True):
        enabled.add("ares_provisional")
    if getattr(config, "USE_TRADETIQ_WAVE", True):
        enabled.add("wave_provisional")
    if getattr(config, "USE_TRADETIQ_SMARTTIQ", False):
        enabled.add("smarttiq")
    if getattr(config, "USE_TRADETIQ_NEXUS", False):
        enabled.add("nexus")
    return enabled


async def _fetch_todays_signals() -> dict | None:
    api_key = getattr(config, "TRADETIQ_API_KEY", "")
    base_url = getattr(config, "TRADETIQ_BASE_URL", "https://tradetiq-production.up.railway.app").rstrip("/")
    if not api_key:
        logger.warning("TRADETIQ_API_KEY not configured")
        return None
    headers = {
        "X-API-Key": api_key,
        "X-Device-Id": "alpaca-bot-01",
        "X-Device-Name": "Alpaca Bot",
    }
    url = f"{base_url}/api/signals/todays-signals-bot"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                logger.warning("Tradetiq rate limited")
                return None
            elif resp.status_code == 403:
                logger.warning("Tradetiq 403 — check API key tier")
                return None
            else:
                logger.warning("Tradetiq returned %d", resp.status_code)
                return None
    except Exception:
        logger.exception("Tradetiq fetch failed")
        return None


def _build_symbol_map(data: dict, enabled: set[str]) -> dict[str, list[dict]]:
    symbol_map: dict[str, list[dict]] = {}
    eod = data.get("eod", {}) or {}
    provisional = data.get("provisional", {}) or {}

    eod_map = {"ripple": "ripple", "wave": "wave", "smarttiq": "smarttiq", "ares": "ares", "nexus": "nexus"}
    for api_key, signal_type in eod_map.items():
        if signal_type not in enabled:
            continue
        for entry in eod.get(api_key, []) or []:
            symbol = entry.get("symbol", "")
            if not symbol:
                continue
            symbol_map.setdefault(symbol, []).append({
                "signal_type": signal_type,
                "quality_score": float(entry.get("quality_score", 0.5) or 0.5),
                "risk_tag": str(entry.get("risk_tag", "Unknown")),
                "label": str(entry.get("label", "Bullish")),
            })

    provisional_map = {
        "ripple_provisional": "ripple_provisional",
        "wave_provisional": "wave_provisional",
        "ares_provisional": "ares_provisional",
    }
    for api_key, signal_type in provisional_map.items():
        if signal_type not in enabled:
            continue
        for entry in provisional.get(api_key, []) or []:
            symbol = entry.get("symbol", "")
            if not symbol:
                continue
            symbol_map.setdefault(symbol, []).append({
                "signal_type": signal_type,
                "quality_score": float(entry.get("quality_score", 0.5) or 0.5),
                "risk_tag": str(entry.get("risk_tag", "Unknown")),
                "label": str(entry.get("label", "Bullish")),
            })

    return symbol_map


async def _ensure_cache_fresh() -> None:
    async with _CACHE_LOCK:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if _CACHE["date"] == today and _CACHE["data"] is not None:
            return
        logger.info("Fetching Tradetiq todays-signals-bot for %s", today)
        data = await _fetch_todays_signals()
        if data:
            enabled = _get_enabled_signal_types()
            _CACHE["date"] = today
            _CACHE["data"] = data
            _CACHE["symbol_map"] = _build_symbol_map(data, enabled)
            _CACHE["ts"] = time.time()
            total = sum(len(v) for v in _CACHE["symbol_map"].values())
            logger.info(
                "Tradetiq cache: %d symbols, %d signals, enabled=%s",
                len(_CACHE["symbol_map"]), total, enabled,
            )
        else:
            logger.warning("Tradetiq fetch failed — keeping stale cache")


class TradetiqAgent(BaseAgent):
    name = "tradetiq"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            await _ensure_cache_fresh()
            symbol_upper = symbol.upper()
            signals = _CACHE["symbol_map"].get(symbol_upper, [])

            if not signals:
                return self.make_signal(
                    symbol=symbol, score=0.5, direction="hold", confidence=0.1,
                    reason="tradetiq: not in today's curated list",
                    metadata={"tradetiq_active": False},
                )

            best = max(signals, key=lambda x: x["quality_score"])
            signal_type = best["signal_type"]
            quality_score = best["quality_score"]
            risk_tag = best["risk_tag"]
            hold_days = _HOLD_DAYS.get(signal_type, 5)

            signal_score = round(0.65 + min(quality_score, 0.5) * 0.50, 4)
            confidence = 0.65
            if quality_score > 0.15:
                confidence += 0.05
            if "Low" in risk_tag:
                confidence += 0.05
            elif "Extreme" in risk_tag:
                confidence -= 0.05
            if len(signals) > 1:
                confidence += 0.05
            confidence = min(0.90, round(confidence, 4))

            signal_names = [s["signal_type"] for s in signals]

            return self.make_signal(
                symbol=symbol,
                score=min(0.92, signal_score),
                direction="buy",
                confidence=confidence,
                reason=f"Tradetiq: {', '.join(signal_names)} | quality={quality_score:.3f} | risk={risk_tag} | hold={hold_days}d",
                metadata={
                    "tradetiq_active": True,
                    "signal_type": signal_type,
                    "signal_types": signal_names,
                    "quality_score": quality_score,
                    "risk_tag": risk_tag,
                    "hold_days": hold_days,
                    "intraday_active": True,
                },
            )

        except Exception:
            self.logger.exception("TradetiqAgent failed for %s", symbol)
            return self.make_signal(
                symbol=symbol, score=0.5, direction="hold", confidence=0.0,
                reason="tradetiq_agent_error",
                metadata={"tradetiq_active": False},
            )