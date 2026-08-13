from __future__ import annotations

"""
ares_agent.py — "Ares" mean-reversion buy/short signal agent.

Based on the Ares signal from Tradetiq 5.0 (Premium tier):
  - Down-streak (Bullish side): 21d hold = 53.9% correct-direction
    (+1.472% mean return, n=69,715)
  - Up-streak (Bearish side): confirmed real underperformance vs baseline
    at both windows (-0.49pp at 21d, -0.80pp at 45d, p<0.0001)

IMPORTANT TAIL RISK: 21-day hold shows 16.2% of down-streak fires with
a >15% move either direction. Longer hold = more time for a real
company-specific event. This is meaningfully higher than Ripple/Wave's
3-4% at their 5-day hold.

Uses the same validated score tables as Tradetiq 5.0's early_signals.py
-- not reimplemented, directly ported.
"""

from typing import Any, Optional
import numpy as np

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars

# -------------------------------------------------------------------
# Score tables — ported directly from Tradetiq 5.0 early_signals.py
# These are the validated mean-reversion scores by streak length.
# Scores > 50 = Bullish lean, < 50 = Bearish lean, 50 = Neutral
# -------------------------------------------------------------------
_MEAN_REVERSION_SCORE_BY_DOWN_STREAK: dict[int, float] = {
    1: 50.5,
    2: 51.2,
    3: 52.4,
    4: 53.1,
    5: 53.9,
    6: 54.2,
    7: 54.8,
    8: 55.0,
    9: 54.6,
    10: 54.1,
}

_UP_STREAK_REVERSAL_SCORE: dict[int, float] = {
    1: 49.8,
    2: 49.2,
    3: 48.6,
    4: 47.9,
    5: 47.3,
    6: 46.8,
    7: 46.4,
    8: 46.1,
    9: 46.0,
    10: 46.2,
}

_NEUTRAL_SCORE = 50.0
_MAX_STREAK_LOOKUP = 10  # beyond this, use the 10-day score


def compute_mean_reversion_score(days_down_streak: int) -> dict:
    streak = min(max(1, days_down_streak), _MAX_STREAK_LOOKUP)
    score = _MEAN_REVERSION_SCORE_BY_DOWN_STREAK.get(streak, _NEUTRAL_SCORE)
    return {"score": score, "is_real": True}


def compute_up_streak_reversal_score(days_up_streak: int) -> dict:
    streak = min(max(1, days_up_streak), _MAX_STREAK_LOOKUP)
    score = _UP_STREAK_REVERSAL_SCORE.get(streak, _NEUTRAL_SCORE)
    return {"score": score, "is_real": True}


def compute_ares_signal(days_down_streak: int | None, days_up_streak: int | None) -> dict:
    """Core Ares logic — same as Tradetiq 5.0's compute_ares_signal()."""
    down_active = days_down_streak is not None and days_down_streak > 0
    up_active = days_up_streak is not None and days_up_streak > 0

    if down_active:
        result = compute_mean_reversion_score(days_down_streak)
    elif up_active:
        result = compute_up_streak_reversal_score(days_up_streak)
    else:
        has_any_real_data = days_down_streak is not None or days_up_streak is not None
        result = {"score": _NEUTRAL_SCORE, "is_real": has_any_real_data}

    score = result["score"]
    if not result.get("is_real", True):
        label = "Neutral"
    elif score > _NEUTRAL_SCORE:
        label = "Bullish"
    elif score < _NEUTRAL_SCORE:
        label = "Bearish"
    else:
        label = "Neutral"

    return {
        "status": "ok",
        "score": score,
        "label": label,
        "is_real": result.get("is_real", True),
    }


# -------------------------------------------------------------------
# Daily bars lookback
# -------------------------------------------------------------------
_DAILY_BARS = 30  # enough to detect streaks up to 10+ days


class AresAgent(BaseAgent):
    name = "ares"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            daily_bars = await get_bars(symbol, timeframe="1Day", limit=_DAILY_BARS)
            if not daily_bars or len(daily_bars) < 5:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="insufficient daily bars",
                    metadata={"ares_active": False},
                )

            closes = [float(b.get("c", 0) or 0) for b in daily_bars]

            # Compute down-streak and up-streak
            days_down_streak = 0
            days_up_streak = 0

            for i in range(len(closes) - 1, 0, -1):
                if closes[i] < closes[i - 1]:
                    if days_up_streak > 0:
                        break
                    days_down_streak += 1
                elif closes[i] > closes[i - 1]:
                    if days_down_streak > 0:
                        break
                    days_up_streak += 1
                else:
                    break

            # Run Ares signal
            ares = compute_ares_signal(
                days_down_streak if days_down_streak > 0 else None,
                days_up_streak if days_up_streak > 0 else None,
            )

            label = ares["label"]
            ares_score = float(ares["score"])
            is_real = bool(ares.get("is_real", False))

            metadata = {
                "ares_active": label != "Neutral",
                "ares_label": label,
                "ares_score": ares_score,
                "days_down_streak": days_down_streak,
                "days_up_streak": days_up_streak,
                "is_real": is_real,
            }

            if not is_real or label == "Neutral":
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason=f"Ares: Neutral (down={days_down_streak}, up={days_up_streak})",
                    metadata=metadata,
                )

            if label == "Bullish":
                # Down-streak bounce — buy signal
                # Scale score and confidence from the ares_score (50-55 range)
                normalized = (ares_score - 50.0) / 5.0  # 0.0 to 1.0
                signal_score = round(0.65 + normalized * 0.15, 4)
                confidence = round(0.55 + normalized * 0.15, 4)
                return self.make_signal(
                    symbol=symbol,
                    score=min(0.90, signal_score),
                    direction="buy",
                    confidence=min(0.80, confidence),
                    reason=f"Ares Bullish: {days_down_streak}-day down-streak (score={ares_score:.1f})",
                    metadata=metadata,
                )

            else:  # Bearish
                # Check if bearish side is enabled
                from backend import config as _cfg
                if not getattr(_cfg, "USE_ARES_BEARISH", False):
                    return self.make_signal(
                        symbol=symbol,
                        score=0.5,
                        direction="hold",
                        confidence=0.1,
                        reason=f"Ares Bearish disabled — {days_up_streak}-day up-streak (score={ares_score:.1f})",
                        metadata=metadata,
                    )
                # Up-streak underperformance — sell/short signal
                normalized = (50.0 - ares_score) / 4.0
                signal_score = round(0.65 + normalized * 0.15, 4)
                confidence = round(0.55 + normalized * 0.15, 4)
                return self.make_signal(
                    symbol=symbol,
                    score=min(0.90, signal_score),
                    direction="sell",
                    confidence=min(0.80, confidence),
                    reason=f"Ares Bearish: {days_up_streak}-day up-streak (score={ares_score:.1f})",
                    metadata=metadata,
                )

        except Exception:
            self.logger.exception("AresAgent failed for %s", symbol)
            return self.make_signal(
                symbol=symbol,
                score=0.5,
                direction="hold",
                confidence=0.0,
                reason="ares_agent_error",
                metadata={"ares_active": False},
            )