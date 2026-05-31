"""
agents/news_agent.py – Watches headlines and sentiment.
"""
from __future__ import annotations
from typing import Optional

from .base import BaseAgent
from ..schemas import AgentSignal
from ..services.news_service import get_news_sentiment


class NewsAgent(BaseAgent):
    name = "news"

    async def analyze(self, symbol: str) -> Optional[AgentSignal]:
        try:
            result = await get_news_sentiment(symbol)
            score = float(result.get("sentiment_score", 0.0))
            headlines = result.get("recent_articles", [])
            direction = "buy" if score > 0.6 else ("sell" if score < 0.4 else "hold")
            return self._make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.7 if headlines else 0.3,
                reason=f"{len(headlines)} headlines; avg_sentiment={score:.2f}",
                headlines=headlines[:3],
            )
        except Exception:
            self.logger.exception("NewsAgent failed for %s", symbol)
            return None