from __future__ import annotations

from typing import Any, Optional

from .base import BaseAgent
from ..services.fundamentals_service import get_snapshot, score_fundamentals


class FundamentalsAgent(BaseAgent):
    name = "fundamentals"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            snapshot = await get_snapshot(symbol)
            if not snapshot:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.3,
                    reason="snapshot unavailable",
                )

            score = float(score_fundamentals(snapshot) or 0.5)
            direction = "buy" if score > 0.6 else "sell" if score < 0.4 else "hold"
            daily = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}

            return self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.65,
                reason=f"fundamental_score={score:.2f}",
                metadata={
                    "close": daily.get("c"),
                    "open": daily.get("o"),
                    "high": daily.get("h"),
                    "low": daily.get("l"),
                },
            )
        except Exception:
            self.logger.exception("FundamentalsAgent failed for %s", symbol)
            return None
