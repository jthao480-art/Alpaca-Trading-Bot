from __future__ import annotations

from typing import Any, Optional

import numpy as np

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars


def ema(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return prices
    k = 2 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append((p * k) + (out[-1] * (1 - k)))
    return out


def rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class MomentumAgent(BaseAgent):
    name = "momentum"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            bars = await get_bars(symbol, timeframe="5Min", limit=60)
            if not bars or len(bars) < 20:
                return self.signal_to_dict(
                    self.make_signal(
                        symbol=symbol,
                        score=0.45,
                        direction="hold",
                        confidence=0.25,
                        reason="insufficient bars",
                        metadata={},
                    )
                )

            closes = [float(b.get("c", 0) or 0) for b in bars]
            volumes = [float(b.get("v", 0) or 0) for b in bars]

            rsi_val = rsi(closes)
            ema9 = ema(closes, 9)[-1]
            ema21 = ema(closes, 21)[-1]
            roc3 = ((closes[-1] - closes[-4]) / closes[-4] * 100.0) if len(closes) >= 4 and closes[-4] else 0.0
            roc6 = ((closes[-1] - closes[-7]) / closes[-7] * 100.0) if len(closes) >= 7 and closes[-7] else 0.0

            recent_vol = float(np.mean(volumes[-3:]))
            prior_vol = float(np.mean(volumes[-10:-3])) if len(volumes) >= 10 else float(np.mean(volumes[:-3])) if len(volumes) > 3 else recent_vol
            if prior_vol <= 0:
                prior_vol = 1.0
            volume_ratio = recent_vol / prior_vol
            volume_trend = recent_vol - prior_vol

            bullish_trend = ema9 > ema21
            early_momentum = roc3 > 0.25 or roc6 > 0.5
            fresh_volume = volume_ratio >= 1.10 and volume_trend > 0

            score = 0.45
            if bullish_trend:
                score += 0.12
            if rsi_val < 62:
                score += 0.08
            if early_momentum:
                score += 0.12
            if fresh_volume:
                score += 0.14
            if volume_ratio >= 1.25:
                score += 0.05

            score = max(0.0, min(1.0, round(score, 4)))

            if bullish_trend and fresh_volume and early_momentum and rsi_val < 68:
                direction = "buy"
                confidence = 0.82
                reason = f"early momentum ROC3={roc3:.2f} ROC6={roc6:.2f} vol={volume_ratio:.2f}"
                score = max(score, 0.60)
            elif not bullish_trend and roc3 < -0.25 and volume_trend < 0:
                direction = "sell"
                confidence = 0.72
                reason = f"momentum fading ROC3={roc3:.2f} vol={volume_ratio:.2f}"
                score = min(score, 0.38)
            else:
                direction = "hold"
                confidence = 0.45
                reason = f"mixed momentum RSI={rsi_val:.1f} ROC3={roc3:.2f} vol={volume_ratio:.2f}"

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "rsi": round(rsi_val, 2),
                    "ema9": round(ema9, 4),
                    "ema21": round(ema21, 4),
                    "roc3": round(roc3, 4),
                    "roc6": round(roc6, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "volume_trend": round(volume_trend, 4),
                    "bullish_trend": bullish_trend,
                    "early_momentum": early_momentum,
                    "fresh_volume": fresh_volume,
                },
            )
            return self.signal_to_dict(signal)
        except Exception:
            self.logger.exception("MomentumAgent failed for %s", symbol)
            return None
