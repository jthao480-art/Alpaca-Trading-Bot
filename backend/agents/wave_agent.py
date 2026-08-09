from __future__ import annotations

"""
wave_agent.py — "Wave" mean-reversion buy signal agent.

Based on the Wave signal from Tradetiq 5.0 (Premium tier):
  - Fires when a stock has been down 3-5 consecutive days (down_streak)
    AND relative_volume >= 1.5 (volume confirmation).
  - Literature-motivated: capitulation-style selling volume during a
    decline is associated with more reliable short-term reversal.
  - 5-day hold window, ~53.7% correct-direction, n=11,896 (2021-2026).

IMPORTANT: Wave's stronger average edge does NOT mean lower risk.
Tail-move rate (|return| > 15% in 5 days) is 4.2% — HIGHER than
Ripple's 3.2%. A volume spike during a decline can mark either a
capitulation buying opportunity OR a genuine negative catalyst.
Stronger edge, not lower risk.

Bullish-only by design. Returns "hold" when conditions not met.
"""

from typing import Any, Optional
import numpy as np

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars

# Wave parameters (validated 2026-07-24)
_STREAK_MIN = 3
_STREAK_MAX = 5
_MIN_RELATIVE_VOLUME = 1.5

# Lookback for relative volume calculation (20 days of daily bars)
_DAILY_BARS = 25
# Lookback for intraday bars to compute today's volume
_INTRADAY_BARS = 60


class WaveAgent(BaseAgent):
    name = "wave"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            # Fetch daily bars to compute down-streak and relative volume
            daily_bars = await get_bars(symbol, timeframe="1Day", limit=_DAILY_BARS)
            if not daily_bars or len(daily_bars) < 6:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="insufficient daily bars",
                    metadata={"wave_active": False},
                )

            closes = [float(b.get("c", 0) or 0) for b in daily_bars]
            volumes = [float(b.get("v", 0) or 0) for b in daily_bars]

            # Compute down-streak (consecutive days closing lower)
            streak = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] < closes[i - 1]:
                    streak += 1
                else:
                    break

            # Compute relative volume: today's volume vs 20-day average
            today_vol = volumes[-1] if volumes else 0.0
            avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
            if avg_vol <= 0:
                avg_vol = 1.0
            relative_volume = today_vol / avg_vol

            # Wave condition: down-streak 3-5 AND relative_volume >= 1.5
            streak_ok = _STREAK_MIN <= streak <= _STREAK_MAX
            vol_ok = relative_volume >= _MIN_RELATIVE_VOLUME
            wave_fires = streak_ok and vol_ok

            metadata = {
                "wave_active": wave_fires,
                "days_down_streak": streak,
                "relative_volume": round(relative_volume, 3),
                "avg_volume": round(avg_vol, 2),
                "today_volume": round(today_vol, 2),
                "streak_ok": streak_ok,
                "vol_ok": vol_ok,
            }

            if wave_fires:
                # Signal fires — high-conviction bullish
                # Score reflects validated edge (~53.7% correct-direction)
                # Confidence scaled by how much volume exceeds threshold
                vol_excess = min(1.0, (relative_volume - _MIN_RELATIVE_VOLUME) / 2.0)
                score = round(0.72 + vol_excess * 0.10, 4)
                confidence = round(0.65 + vol_excess * 0.10, 4)

                return self.make_signal(
                    symbol=symbol,
                    score=min(0.95, score),
                    direction="buy",
                    confidence=min(0.85, confidence),
                    reason=f"Wave: {streak}-day down-streak with {relative_volume:.2f}x volume",
                    metadata=metadata,
                )
            else:
                # Signal does not fire
                reason_parts = []
                if not streak_ok:
                    reason_parts.append(f"streak={streak} (need {_STREAK_MIN}-{_STREAK_MAX})")
                if not vol_ok:
                    reason_parts.append(f"rel_vol={relative_volume:.2f} (need >={_MIN_RELATIVE_VOLUME})")
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="Wave: " + ", ".join(reason_parts) if reason_parts else "Wave: conditions not met",
                    metadata=metadata,
                )

        except Exception:
            self.logger.exception("WaveAgent failed for %s", symbol)
            return self.make_signal(
                symbol=symbol,
                score=0.5,
                direction="hold",
                confidence=0.0,
                reason="wave_agent_error",
                metadata={"wave_active": False},
            )