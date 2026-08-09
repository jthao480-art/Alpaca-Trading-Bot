from __future__ import annotations

from typing import Any, Optional

from .base import BaseAgent
from ..services.newsservice import get_news_sentiment


class NewsAgentV2(BaseAgent):
    name = "news_v2"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            result = await get_news_sentiment(symbol)
            if not isinstance(result, dict):
                result = {}

            score = float(result.get("score", 0.5) or 0.5)
            headlines = result.get("headlines", []) or []
            direction = "buy" if score >= 0.6 else "sell" if score <= 0.4 else "hold"

            return self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.7 if headlines else 0.3,
                reason=f"{len(headlines)} headlines avg sentiment {score:.2f}",
                metadata={
                    "headlines": headlines[:3],
                    "count": result.get("count", 0),
                    "error": result.get("error"),
                    "raw": result.get("raw"),
                },
            )
        except Exception:
            self.logger.exception("NewsAgentV2 failed for %s", symbol)
            return None
