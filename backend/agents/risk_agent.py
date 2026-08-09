from __future__ import annotations

from typing import Any, Optional

from .base import BaseAgent
from ..services.bars_service import get_latest_quote


class RiskAgent(BaseAgent):
    name = "risk"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            quote = await get_latest_quote(symbol)
            if not isinstance(quote, dict):
                quote = {}

            bid = float(quote.get("bp", 0.0) or 0.0)
            ask = float(quote.get("ap", 0.0) or 0.0)
            spread = (ask - bid) if ask > bid else 0.0
            spread_pct = (spread / ((ask + bid) / 2.0)) if (ask + bid) > 0 else 0.0

            direction = "sell" if spread_pct > 0.01 else "hold"
            score = 0.3 if spread_pct > 0.01 else 0.5

            return self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.7,
                reason=f"spread_pct={spread_pct:.4f}",
                metadata={
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "spread_pct": spread_pct,
                    "raw": quote,
                },
            )
        except Exception:
            self.logger.exception("RiskAgent failed for %s", symbol)
            return None
