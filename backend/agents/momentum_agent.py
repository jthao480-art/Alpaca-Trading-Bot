"""
agents/momentum_agent.py – Watches price action (RSI, EMA crossover).
"""
from __future__ import annotations
from typing import List, Optional

import numpy as np

from .base import BaseAgent
from ..schemas import AgentSignal
from ..services.bars_service import get_bars


def _ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return prices
    k = 2 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


def _rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-period - 1:])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


class MomentumAgent(BaseAgent):
    name = "momentum"

    async def analyze(self, symbol: str) -> Optional[AgentSignal]:
        try:
            bars = await get_bars(symbol, timeframe="5Min", limit=60)
            if len(bars) < 20:
                self.logger.warning("MomentumAgent: insufficient bars for %s", symbol)
                return self._make_signal(symbol, 0.5, "hold", 0.3, "insufficient data")

            closes = [float(b["c"]) for b in bars]
            rsi = _rsi(closes)
            ema9 = _ema(closes, 9)[-1]
            ema21 = _ema(closes, 21)[-1]

            # Score: RSI normalised + EMA cross signal
            rsi_score = 1 - (rsi / 100)   # low RSI → potential buy
            ema_score = 1.0 if ema9 > ema21 else 0.0
            score = 0.5 * rsi_score + 0.5 * ema_score

            if score > 0.6:
                direction = "buy"
            elif score < 0.4:
                direction = "sell"
            else:
                direction = "hold"

            return self._make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=0.75,
                reason=f"RSI={rsi:.1f} EMA9={ema9:.2f} EMA21={ema21:.2f}",
                rsi=round(rsi, 2),
                ema9=round(ema9, 4),
                ema21=round(ema21, 4),
            )
        except Exception:
            self.logger.exception("MomentumAgent failed for %s", symbol)
            return None




