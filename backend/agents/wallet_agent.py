"""
agents/wallet_agent.py - Portfolio risk and concentration agent.

Analyzes current portfolio state to determine if buying more is wise.
Answers: "Given current exposure, cash, and concentration, should we buy this?"

Signal logic:
- Low exposure + available cash + symbol not concentrated = buy-friendly
- High exposure + concentrated in sector + near daily loss limit = sell-friendly  
- Already holding this symbol = hold (avoid doubling up)
"""
from __future__ import annotations

from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.execution import (
    _get_account,
    _get_open_positions,
    MAX_LEVERAGE,
    DAILY_LOSS_LIMIT,
)


class WalletAgent(BaseAgent):
    name = "wallet"

    # Class-level cache — shared across all symbols in a scan cycle
    _acct_cache: dict = {}
    _positions_cache: list = []
    _cache_ts: float = 0.0
    _CACHE_TTL: float = 30.0  # seconds

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            import time
            now = time.time()
            if now - WalletAgent._cache_ts > WalletAgent._CACHE_TTL or not WalletAgent._acct_cache:
                WalletAgent._acct_cache = await _get_account() or {}
                WalletAgent._positions_cache = await _get_open_positions()
                WalletAgent._cache_ts = now
            acct = WalletAgent._acct_cache
            positions = WalletAgent._positions_cache

            if not acct:
                return self.signal_to_dict(self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.25,
                    reason="account unavailable",
                    metadata={},
                ))

            equity = float(acct.get("equity") or 0)
            buying_power = float(acct.get("buying_power") or 0)
            daily_pl = equity - float(acct.get("last_equity") or equity)

            # Gross exposure
            gross_long = sum(
                float(p.get("market_value", 0))
                for p in positions
                if float(p.get("market_value", 0)) > 0
            )
            exposure_ratio = gross_long / equity if equity > 0 else 0.0
            exposure_headroom = (equity * MAX_LEVERAGE) - gross_long

            # Check if already holding this symbol
            already_holding = any(
                str(p.get("symbol", "")).upper() == symbol.upper()
                for p in positions
            )

            # Daily P&L as % of equity
            daily_pl_pct = daily_pl / equity if equity > 0 else 0.0
            near_loss_limit = daily_pl <= (DAILY_LOSS_LIMIT * 0.75)

            # Position count
            position_count = len(positions)

            # ── Scoring ──────────────────────────────────────────────────
            score = 0.5  # neutral base

            # Already holding — don't double up
            if already_holding:
                return self.signal_to_dict(self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.60,
                    reason=f"already_holding {symbol}",
                    metadata={
                        "already_holding": True,
                        "exposure_ratio": round(exposure_ratio, 3),
                        "daily_pl_pct": round(daily_pl_pct, 4),
                    },
                ))

            # Near daily loss limit — be cautious
            if near_loss_limit:
                score -= 0.15

            # Exposure headroom
            if exposure_headroom > 20000:
                score += 0.10  # plenty of room
            elif exposure_headroom > 10000:
                score += 0.05  # some room
            elif exposure_headroom < 0:
                score -= 0.20  # over cap

            # Buying power available
            if buying_power > 50000:
                score += 0.08
            elif buying_power > 20000:
                score += 0.04
            elif buying_power < 5000:
                score -= 0.10

            # Portfolio concentration — too many positions = cautious
            if position_count < 20:
                score += 0.05  # room for more
            elif position_count > 40:
                score -= 0.05  # getting crowded

            # Daily P&L positive = good environment for buying
            if daily_pl_pct > 0.005:
                score += 0.05
            elif daily_pl_pct < -0.01:
                score -= 0.08

            score = round(max(0.0, min(1.0, score)), 4)

            # Direction
            if score >= 0.60:
                direction = "buy"
                confidence = min(0.80, 0.50 + (score - 0.60) * 1.5)
                reason = (
                    f"wallet_favorable exposure={exposure_ratio:.2f}x "
                    f"headroom=${exposure_headroom:.0f} "
                    f"bp=${buying_power:.0f} daily_pl={daily_pl_pct:.2%}"
                )
            elif score <= 0.40:
                direction = "sell"
                confidence = min(0.80, 0.50 + (0.40 - score) * 1.5)
                reason = (
                    f"wallet_cautious exposure={exposure_ratio:.2f}x "
                    f"headroom=${exposure_headroom:.0f} "
                    f"near_loss_limit={near_loss_limit}"
                )
            else:
                direction = "hold"
                confidence = 0.40
                reason = (
                    f"wallet_neutral exposure={exposure_ratio:.2f}x "
                    f"positions={position_count}"
                )

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "already_holding": False,
                    "exposure_ratio": round(exposure_ratio, 3),
                    "exposure_headroom": round(exposure_headroom, 2),
                    "buying_power": round(buying_power, 2),
                    "position_count": position_count,
                    "daily_pl": round(daily_pl, 2),
                    "daily_pl_pct": round(daily_pl_pct, 4),
                    "near_loss_limit": near_loss_limit,
                },
            )
            return self.signal_to_dict(signal)

        except Exception:
            self.logger.exception("WalletAgent failed for %s", symbol)
            return None