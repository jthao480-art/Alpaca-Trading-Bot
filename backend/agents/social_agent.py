"""
backend/agents/social_agent.py

Social spike agent — detects unusual social media activity on StockTwits
as an early leading indicator before price moves.

Signal logic:
- Strong bullish spike (score > 0.70) → buy signal
- Strong bearish spike (score < 0.30) → sell signal  
- Neutral or no spike → hold

Weight in new architecture: 30% (largest single weight — lead indicator)
"""

from __future__ import annotations

from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.services.stocktwits_service import (
    get_stocktwits_sentiment,
    get_social_spike_score,
)


class SocialAgent(BaseAgent):
    name = "social"

    # Thresholds for signal generation
    BULLISH_THRESHOLD = 0.65   # score above this = buy signal
    BEARISH_THRESHOLD = 0.35   # score below this = sell signal
    MIN_MESSAGES = 3           # minimum messages to trust signal
    SPIKE_REQUIRED = False     # if True, only signal on volume spikes

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            data = await get_stocktwits_sentiment(symbol)
            score = await get_social_spike_score(symbol)

            message_count = int(data.get("message_count", 0))
            bullish_pct = float(data.get("bullish_pct", 0.0))
            bearish_pct = float(data.get("bearish_pct", 0.0))
            spike = bool(data.get("spike", False))
            spike_ratio = float(data.get("spike_ratio", 1.0))
            velocity = float(data.get("velocity_score", 0.0))
            error = data.get("error")

            # Not enough data to generate a signal
            if message_count < self.MIN_MESSAGES or error in ("not_found", "timeout", "error"):
                return self._hold_signal(
                    symbol=symbol,
                    score=score,
                    reason=f"insufficient_data messages={message_count} error={error}",
                    data=data,
                )

            # Generate directional signal
            if score >= self.BULLISH_THRESHOLD:
                direction = "buy"
                confidence = min(0.95, 0.60 + (score - self.BULLISH_THRESHOLD) * 1.5)
                if spike:
                    confidence = min(0.95, confidence + 0.10)  # boost on spike
                reason = (
                    f"social_bullish score={score:.2f} bullish={bullish_pct:.0%} "
                    f"spike={spike} spike_ratio={spike_ratio:.1f}x velocity={velocity:.1f}x"
                )
            elif score <= self.BEARISH_THRESHOLD:
                direction = "sell"
                confidence = min(0.95, 0.60 + (self.BEARISH_THRESHOLD - score) * 1.5)
                if spike:
                    confidence = min(0.95, confidence + 0.10)
                reason = (
                    f"social_bearish score={score:.2f} bearish={bearish_pct:.0%} "
                    f"spike={spike} spike_ratio={spike_ratio:.1f}x velocity={velocity:.1f}x"
                )
            else:
                return self._hold_signal(
                    symbol=symbol,
                    score=score,
                    reason=f"social_neutral score={score:.2f} messages={message_count}",
                    data=data,
                )

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "social_score": round(score, 4),
                    "message_count": message_count,
                    "bullish_pct": round(bullish_pct, 3),
                    "bearish_pct": round(bearish_pct, 3),
                    "spike": spike,
                    "spike_ratio": round(spike_ratio, 2),
                    "velocity": round(velocity, 2),
                    "error": error,
                },
            )
            return self.signal_to_dict(signal)

        except Exception:
            self.logger.exception("SocialAgent failed for %s", symbol)
            return None

    def _hold_signal(
        self,
        symbol: str,
        score: float,
        reason: str,
        data: dict,
    ) -> dict[str, Any]:
        signal = self.make_signal(
            symbol=symbol,
            score=score,
            direction="hold",
            confidence=0.30,
            reason=reason,
            metadata={
                "social_score": round(score, 4),
                "message_count": int(data.get("message_count", 0)),
                "bullish_pct": round(float(data.get("bullish_pct", 0.0)), 3),
                "bearish_pct": round(float(data.get("bearish_pct", 0.0)), 3),
                "spike": bool(data.get("spike", False)),
                "spike_ratio": round(float(data.get("spike_ratio", 1.0)), 2),
                "velocity": round(float(data.get("velocity", 1.0)), 2),
                "error": data.get("error"),
            },
        )
        return self.signal_to_dict(signal)