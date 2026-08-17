from __future__ import annotations

"""
tradetiq_agent.py — Tradetiq API signal agent.

Calls Tradetiq's /api/composite/{symbol} endpoint to get composite
signals including technical, smart money, social sentiment, and
early signals (social spike, unusual volume, news velocity, insider cluster).

Hold window: 21 days (matches Tradetiq's composite validation)
Bullish-only by design — composite Bearish side not validated for bot use.

Configure via .env:
    TRADETIQ_API_KEY=tdk_...
    TRADETIQ_BASE_URL=https://tradetiq-production.up.railway.app
    USE_TRADETIQ_AGENT=false
"""

import asyncio
from typing import Any, Optional

import httpx

from backend.agents.base import BaseAgent
from backend import config

# Thresholds
_BULLISH_THRESHOLD = 65       # composite_score >= this → Bullish
_MIN_TECHNICAL_SCORE = 60     # technical component score minimum
_CACHE: dict[str, dict] = {}  # simple in-memory cache
_CACHE_TTL_SECONDS = 300      # 5 minutes


class TradetiqAgent(BaseAgent):
    name = "tradetiq"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = getattr(config, "TRADETIQ_API_KEY", "")
        self.base_url = getattr(config, "TRADETIQ_BASE_URL", "https://tradetiq-production.up.railway.app").rstrip("/")
        self.headers = {
            "X-API-Key": self.api_key,
            "X-Device-Id": "alpaca-bot-01",
            "X-Device-Name": "Alpaca Bot",
        }

    async def _fetch_composite(self, symbol: str) -> dict | None:
        """Fetch composite signal from Tradetiq API with caching."""
        import time
        cache_key = symbol.upper()
        cached = _CACHE.get(cache_key)
        if cached and (time.time() - cached.get("_ts", 0)) < _CACHE_TTL_SECONDS:
            return cached

        url = f"{self.base_url}/api/composite/{symbol}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self.headers)
                if resp.status_code == 200:
                    data = resp.json()
                    data["_ts"] = time.time()
                    _CACHE[cache_key] = data
                    return data
                elif resp.status_code == 429:
                    self.logger.warning("Tradetiq rate limited for %s", symbol)
                    return None
                else:
                    self.logger.debug("Tradetiq %s returned %d", symbol, resp.status_code)
                    return None
        except Exception:
            self.logger.exception("Tradetiq API call failed for %s", symbol)
            return None

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            if not self.api_key:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.0,
                    reason="tradetiq: no API key configured",
                    metadata={"tradetiq_active": False},
                )

            data = await self._fetch_composite(symbol)
            if not data:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="tradetiq: no data available",
                    metadata={"tradetiq_active": False},
                )

            composite_score = int(data.get("composite_score", 50) or 50)
            composite_label = str(data.get("composite_label", "Neutral"))
            components = data.get("components", {}) or {}

            # Extract component scores
            technical = components.get("technical", {}) or {}
            smart_money = components.get("smart_money", {}) or {}
            social = components.get("social_sentiment", {}) or {}
            news = components.get("news_sentiment", {}) or {}
            early = components.get("early_signal", {}) or {}

            tech_score = int(technical.get("signal_score", 50) or 50)
            tech_label = str(technical.get("signal_label", "Neutral"))
            sm_score = int(smart_money.get("score", 50) or 50)
            sm_label = str(smart_money.get("label", "Neutral"))
            social_label = str(social.get("sentiment_label", "Neutral"))
            news_score = int(news.get("score", 50) or 50)
            early_score = int(early.get("score", 50) or 50)
            strong_signals = early.get("strong_signals", []) or []

            metadata = {
                "tradetiq_active": composite_label == "Bullish",
                "composite_score": composite_score,
                "composite_label": composite_label,
                "technical_score": tech_score,
                "technical_label": tech_label,
                "smart_money_score": sm_score,
                "smart_money_label": sm_label,
                "social_label": social_label,
                "news_score": news_score,
                "early_score": early_score,
                "strong_signals": strong_signals,
            }

            if composite_label == "Bullish" and composite_score >= _BULLISH_THRESHOLD:
                # Scale signal score from composite (65-100 → 0.65-0.95)
                normalized = (composite_score - _BULLISH_THRESHOLD) / (100 - _BULLISH_THRESHOLD)
                signal_score = round(0.65 + normalized * 0.30, 4)

                # Boost confidence if multiple components agree
                confidence = 0.60
                if tech_label == "Bullish":
                    confidence += 0.08
                if sm_label == "Bullish":
                    confidence += 0.08
                if strong_signals:
                    confidence += 0.05
                if social_label == "Bullish":
                    confidence += 0.04
                confidence = min(0.92, round(confidence, 4))

                reason_parts = [f"Tradetiq composite={composite_score}"]
                if tech_label == "Bullish":
                    reason_parts.append(f"tech={tech_score}")
                if sm_label == "Bullish":
                    reason_parts.append("smart_money=Bullish")
                if strong_signals:
                    reason_parts.append(f"signals={','.join(strong_signals)}")

                return self.make_signal(
                    symbol=symbol,
                    score=min(0.95, signal_score),
                    direction="buy",
                    confidence=confidence,
                    reason=" | ".join(reason_parts),
                    metadata=metadata,
                )
            else:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason=f"Tradetiq: {composite_label} (score={composite_score})",
                    metadata=metadata,
                )

        except Exception:
            self.logger.exception("TradetiqAgent failed for %s", symbol)
            return self.make_signal(
                symbol=symbol,
                score=0.5,
                direction="hold",
                confidence=0.0,
                reason="tradetiq_agent_error",
                metadata={"tradetiq_active": False},
            )