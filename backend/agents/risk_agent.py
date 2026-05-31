"""
agents/risk_agent.py – Hard veto conditions and safety rules.
This agent is the last line of defence and cannot be disabled.
"""
from __future__ import annotations
from typing import Optional, Tuple

from ..services.bars_service import get_latest_quote
from ..services.wallet_service import get_account
from .. import config


class RiskAgent:
    """
    RiskAgent is NOT a BaseAgent – it returns (vetoed: bool, reason: str).
    It is called by the orchestrator AFTER all other agents have produced signals.
    """

    async def check(
        self,
        symbol: str,
        action: str,
        proposed_price: float,
        daily_pnl: float,
        has_position: bool,
    ) -> Tuple[bool, str]:
        """
        Returns (True, reason) if the trade should be blocked.
        Returns (False, "") if the trade is safe to proceed.
        """
        # 1. Daily loss limit
        if daily_pnl < 0 and abs(daily_pnl) >= config.DAILY_LOSS_LIMIT_USD:
            return True, f"Daily loss limit reached: ${abs(daily_pnl):.2f}"

        # 2. Spread / liquidity check on buy
        if action == "buy":
            quote = await get_latest_quote(symbol)
            if quote:
                bid = float(quote.get("bp", 0) or 0)
                ask = float(quote.get("ap", 0) or 0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ask
                    if spread_pct > getattr(config, "MAX_SPREAD_PCT", getattr(config, "MAX_SPREAD", 0.0)):
                        return True, f"Spread too wide: {spread_pct:.3%}"

        # 3. Already have a position – don't double-buy
        if action == "buy" and has_position:
            return True, f"Already holding position in {symbol}"

        # 4. Account buying power check on buy
        if action == "buy":
            account = await get_account()
            if account:
                bp = float(account.get("buying_power", 0) or 0)
                max_position_size = getattr(config, "MAX_POSITION_SIZE_USD", getattr(config, "MAX_POSITION_SIZE", 0))
                if bp < max_position_size * 0.5:
                    return True, f"Insufficient buying power: ${bp:.2f}"

        return False, ""



