from __future__ import annotations

from typing import Any, Optional

from .base import BaseAgent
from ..trade_manager import TradeManager


class PortfolioAgent(BaseAgent):
    name = "portfolio"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            tm = TradeManager()
            positions = tm.get_open_positions() or []
            open_orders = tm.get_open_orders() or []

            position = next((p for p in positions if getattr(p, "symbol", None) == symbol), None)
            related_orders = [o for o in open_orders if getattr(o, "symbol", None) == symbol]

            if position is None:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.4,
                    reason="no portfolio exposure",
                    metadata={"has_position": False, "open_orders": len(related_orders)},
                )

            qty = float(getattr(position, "qty", 0.0) or 0.0)
            market_value = float(getattr(position, "market_value", 0.0) or 0.0)
            unrealized_pl = float(getattr(position, "unrealized_pl", 0.0) or 0.0)
            unrealized_plpc = float(getattr(position, "unrealized_plpc", 0.0) or 0.0)

            if unrealized_plpc > 0.03:
                direction = "buy"
                score = 0.7
                confidence = 0.75
                reason = "profitable position and room to add"
            elif unrealized_plpc < -0.03:
                direction = "sell"
                score = 0.7
                confidence = 0.75
                reason = "losing position and risk reduction favored"
            else:
                direction = "hold"
                score = 0.5
                confidence = 0.6
                reason = "portfolio position is neutral"

            return self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "has_position": True,
                    "qty": qty,
                    "market_value": market_value,
                    "unrealized_pl": unrealized_pl,
                    "unrealized_plpc": unrealized_plpc,
                    "open_orders": len(related_orders),
                },
            )
        except Exception:
            self.logger.exception("PortfolioAgent failed for %s", symbol)
            return None
