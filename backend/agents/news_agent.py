from __future__ import annotations

from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.services.newsservice import get_news_sentiment


class NewsAgent(BaseAgent):
    name = "news"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            result = await get_news_sentiment(symbol)

            score = float(result.get("score", 0.5) or 0.5)
            label = str(result.get("label", "neutral") or "neutral").lower()
            confidence = float(result.get("confidence", 0.0) or 0.0)
            headlines = result.get("headlines", []) or []
            count = int(result.get("count", 0) or 0)
            raw = result.get("raw")
            error = result.get("error")
            details = result.get("details", []) or []

            if label == "positive" and count > 0:
                direction = "buy"
                reason = f"finbert positive confidence={confidence:.2f} score={score:.2f}"
                signal_confidence = min(0.95, max(0.60, confidence if confidence > 0 else 0.65))
            elif label == "negative" and count > 0:
                direction = "sell"
                reason = f"finbert negative confidence={confidence:.2f} score={score:.2f}"
                signal_confidence = min(0.95, max(0.60, confidence if confidence > 0 else 0.65))
            else:
                direction = "hold"
                reason = f"finbert neutral confidence={confidence:.2f} score={score:.2f}"
                signal_confidence = 0.40 if count > 0 else 0.25

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=signal_confidence,
                reason=reason,
                metadata={
                    "headlines": headlines[:3],
                    "count": count,
                    "news_score": round(score, 4),
                    "news_label": label,
                    "news_confidence": round(confidence, 4),
                    "velocity_ratio": round(float(result.get("velocity_ratio", 1.0)), 2),
                    "velocity_score": round(float(result.get("velocity_score", 0.25)), 3),
                    "news_spike": bool(result.get("news_spike", False)),
                    "recent_count": int(result.get("recent_count", 0)),
                    "raw": raw,
                    "error": error,
                    "details": details[:10],
                    "gatekeeper": False,
                },
            )
            return self.signal_to_dict(signal)

        except Exception:
            self.logger.exception("NewsAgent failed for %s", symbol)
            return None