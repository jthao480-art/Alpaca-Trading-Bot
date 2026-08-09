from __future__ import annotations

from typing import Any, Optional

import numpy as np

from backend.agents.base import BaseAgent
from backend import config
from backend.services.bars_service import get_bars


class VolumeAgent(BaseAgent):
    name = "volume"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            bars = await get_bars(symbol, timeframe="5Min", limit=40)
            if not bars or len(bars) < 12:
                return self.signal_to_dict(
                    self.make_signal(
                        symbol=symbol,
                        score=0.5,
                        direction="hold",
                        confidence=0.25,
                        reason="insufficient bars",
                        metadata={},
                    )
                )

            closes = [float(bar.get("c", 0) or 0) for bar in bars]
            highs = [float(bar.get("h", 0) or 0) for bar in bars]
            lows = [float(bar.get("l", 0) or 0) for bar in bars]
            volumes = [float(bar.get("v", 0) or 0) for bar in bars]

            recent_vol = float(np.mean(volumes[-3:]))
            prior_vol = float(np.mean(volumes[-10:-3])) if len(volumes) >= 10 else float(np.mean(volumes[:-3])) if len(volumes) > 3 else recent_vol
            avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0

            if prior_vol <= 0:
                prior_vol = 1.0
            if avg_vol <= 0:
                avg_vol = 1.0

            volume_ratio = recent_vol / avg_vol
            volume_acceleration = recent_vol / prior_vol
            volume_slope = recent_vol - prior_vol

            price_slope = closes[-1] - closes[-4] if len(closes) >= 4 else closes[-1] - closes[0]
            range_high = max(highs[-10:]) if highs else closes[-1]
            range_low = min(lows[-10:]) if lows else closes[-1]
            breakout = closes[-1] >= range_high * 0.995 if range_high > 0 else False

            score = 0.5
            if volume_ratio >= 1.15:
                score += 0.08
            if volume_ratio >= 1.35:
                score += 0.08
            if volume_acceleration >= 1.10:
                score += 0.08
            if volume_slope > 0:
                score += 0.05
            if price_slope > 0:
                score += 0.06
            if breakout:
                score += 0.10

            score = max(0.0, min(1.0, round(score, 4)))

            if volume_slope < 0 and volume_ratio < 0.95:
                direction = "sell"
                # Scale sell score based on severity of volume fade
                fade_severity = max(0.0, 1.0 - volume_ratio)  # 0.05 fade = 0.05, 0.50 fade = 0.50
                accel_penalty = max(0.0, 1.0 - volume_acceleration)
                sell_score = min(1.0, 0.50 + (fade_severity * 0.30) + (accel_penalty * 0.20))
                score = round(sell_score, 4)
                confidence = min(0.95, 0.70 + fade_severity * 0.25)
                reason = f"volume_fade ratio={volume_ratio:.2f} accel={volume_acceleration:.2f} fade={fade_severity:.2f}"
            elif breakout and volume_ratio >= 1.15 and volume_acceleration >= 1.05 and price_slope > 0:
                direction = "buy"
                confidence = 0.82
                reason = f"early breakout volume_ratio={volume_ratio:.2f} accel={volume_acceleration:.2f}"
                score = max(score, 0.60)
            else:
                direction = "hold"
                confidence = 0.55 if volume_ratio >= 1.0 else 0.35
                reason = f"no clean volume expansion ratio={volume_ratio:.2f} accel={volume_acceleration:.2f}"

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "volume_ratio": round(volume_ratio, 4),
                    "volume_acceleration": round(volume_acceleration, 4),
                    "volume_slope": round(volume_slope, 4),
                    "recent_volume": round(recent_vol, 2),
                    "average_volume": round(avg_vol, 2),
                    "prior_volume": round(prior_vol, 2),
                    "price_slope": round(price_slope, 4),
                    "breakout": breakout,
                    "range_high": round(range_high, 4),
                    "range_low": round(range_low, 4),
                    "min_volume": config.MIN_VOLUME,
                },
            )
            return self.signal_to_dict(signal)
        except Exception:
            self.logger.exception("VolumeAgent failed for %s", symbol)
            return None
