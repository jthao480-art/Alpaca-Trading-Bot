"""
agents/insider_agent.py - Watches insider filings via SEC EDGAR.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.services.insider_service import get_insider_sentiment


class InsiderAgent(BaseAgent):
    name = "insider"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            result = await get_insider_sentiment(symbol, limit=20)

            score = float(result.get("score", 0.5))
            count = int(result.get("count", 0))
            headlines = result.get("headlines", [])
            error = result.get("error")

            if count == 0:
                signal = self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.25,
                    reason="no insider filings found",
                    metadata={"filing_count": 0, "headlines": []},
                )
                return self.signal_to_dict(signal)

            # Direction based on filing frequency score
            if score >= 0.65:
                direction = "buy"
                confidence = min(0.85, 0.55 + (score - 0.65) * 1.5)
                reason = f"insider_active filings={count} score={score:.2f}"
            elif score <= 0.40:
                direction = "sell"
                confidence = 0.55
                reason = f"insider_bearish filings={count} score={score:.2f}"
            else:
                direction = "hold"
                confidence = 0.40
                reason = f"insider_neutral filings={count} score={score:.2f}"

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "filing_count": count,
                    "insider_score": score,
                    "insider_adjustment": score - 0.5,
                    "headlines": headlines[:5],
                    "error": error,
                },
            )
            return self.signal_to_dict(signal)

        except Exception:
            self.logger.exception("InsiderAgent failed for %s", symbol)
            return None