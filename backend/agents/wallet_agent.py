"""
agents/wallet_agent.py – Watches portfolio health and available capital.
"""
from __future__ import annotations
from typing import Optional

from .base import BaseAgent
from ..schemas import AgentSignal
from ..services.wallet_service import get_portfolio_health


class WalletAgent(BaseAgent):
    name = "wallet"

    async def analyze(self, symbol: str) -> Optional[AgentSignal]:
        try:
            health = await get_portfolio_health()
            direction = "buy" if health > 0.5 else ("sell" if health < 0.25 else "hold")
            reason = f"portfolio_health={health:.2f}"
            return self._make_signal(
                symbol=symbol,
                score=health,
                direction=direction,
                confidence=0.8,
                reason=reason,
            )
        except Exception:
            self.logger.exception("WalletAgent failed for %s", symbol)
            return None




