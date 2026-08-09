from __future__ import annotations

from typing import Any, Optional

from .base import BaseAgent
from ..services.bars_service import get_bars


class TechnicalAgent(BaseAgent):
    name = "technical"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            bars = await get_bars(symbol, timeframe="5Min", limit=50)
            if not isinstance(bars, list) or len(bars) < 10:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.3,
                    reason="insufficient bars",
                )

            closes = [float(b["c"]) for b in bars if isinstance(b, dict) and "c" in b]
            if len(closes) < 10:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.3,
                    reason="insufficient close data",
                )

            recent = closes[-1]
            prior = closes[-5]
            change = (recent - prior) / prior if prior > 0 else 0.0
            direction = "buy" if change > 0.01 else "sell" if change < -0.01 else "hold"
            score = max(0.0, min(1.0, 0.5 + change * 5))

            return self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.65,
                reason=f"5-bar change={change:.4f}",
                metadata={"change": change, "recent": recent, "prior": prior},
            )
        except Exception:
            self.logger.exception("TechnicalAgent failed for %s", symbol)
            return None
