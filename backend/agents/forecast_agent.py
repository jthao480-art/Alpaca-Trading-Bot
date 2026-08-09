"""
backend/agents/forecast_agent.py

Price forecast agent using technical indicators from existing bar data.
No new APIs needed — uses the same 5Min bars as momentum and volume agents.

Signal logic based on:
1. EMA crossover (9/21) — trend direction
2. RSI — overbought/oversold
3. VWAP deviation — price vs fair value
4. Price momentum projection — rate of change extrapolation
5. Higher timeframe alignment (30min trend)
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars


def _ema(prices: list[float], period: int) -> list[float]:
    if not prices or len(prices) < 2:
        return prices
    k = 2 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append((p * k) + (out[-1] * (1 - k)))
    return out


def _rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains.mean())
    avg_loss = float(losses.mean())
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _vwap(bars: list[dict]) -> float:
    """Calculate VWAP from bars."""
    total_volume = sum(float(b.get("v", 0)) for b in bars)
    if total_volume <= 0:
        return 0.0
    total_pv = sum(float(b.get("vw", 0)) * float(b.get("v", 0)) for b in bars)
    return total_pv / total_volume


def _momentum_projection(closes: list[float], periods: int = 5) -> float:
    """
    Project price direction using linear regression slope.
    Returns normalized slope: positive = uptrend, negative = downtrend.
    """
    if len(closes) < periods:
        return 0.0
    recent = closes[-periods:]
    x = np.arange(len(recent))
    try:
        slope = float(np.polyfit(x, recent, 1)[0])
        # Normalize by price level
        price_level = recent[-1] if recent[-1] > 0 else 1.0
        return slope / price_level * 100
    except Exception:
        return 0.0


class ForecastAgent(BaseAgent):
    name = "forecast"

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            # Fetch 60 bars for indicators
            bars = await get_bars(symbol, timeframe="5Min", limit=60)
            if not bars or len(bars) < 20:
                return self.signal_to_dict(
                    self.make_signal(
                        symbol=symbol,
                        score=0.5,
                        direction="hold",
                        confidence=0.25,
                        reason="insufficient bars for forecast",
                        metadata={},
                    )
                )

            closes = [float(b.get("c", 0) or 0) for b in bars]
            highs = [float(b.get("h", 0) or 0) for b in bars]
            lows = [float(b.get("l", 0) or 0) for b in bars]
            volumes = [float(b.get("v", 0) or 0) for b in bars]

            current_price = closes[-1]
            if current_price <= 0:
                return None

            # ── Indicators ──────────────────────────────────────────────────
            ema9 = _ema(closes, 9)
            ema21 = _ema(closes, 21)
            ema9_now = ema9[-1]
            ema21_now = ema21[-1]
            ema9_prev = ema9[-2] if len(ema9) >= 2 else ema9_now
            ema21_prev = ema21[-2] if len(ema21) >= 2 else ema21_now

            rsi_val = _rsi(closes)
            vwap_val = _vwap(bars)
            vwap_deviation = (current_price - vwap_val) / vwap_val if vwap_val > 0 else 0.0

            # Momentum projection (5 bar slope)
            momentum_slope = _momentum_projection(closes, periods=5)

            # Higher timeframe — use last 12 bars (1 hour) for trend
            ht_closes = closes[-12:]
            ht_ema9 = _ema(ht_closes, 9)[-1] if len(ht_closes) >= 9 else closes[-1]
            ht_trend_up = ht_closes[-1] > ht_ema9

            # ── Signal scoring ──────────────────────────────────────────────
            bullish_signals = 0
            bearish_signals = 0
            total_signals = 0

            # 1. EMA crossover
            ema_cross_up = ema9_now > ema21_now and ema9_prev <= ema21_prev
            ema_cross_down = ema9_now < ema21_now and ema9_prev >= ema21_prev
            ema_bullish = ema9_now > ema21_now
            ema_bearish = ema9_now < ema21_now

            total_signals += 2
            if ema_bullish:
                bullish_signals += 1
            else:
                bearish_signals += 1
            if ema_cross_up:
                bullish_signals += 1
            elif ema_cross_down:
                bearish_signals += 1

            # 2. RSI
            total_signals += 1
            if rsi_val < 35:
                bullish_signals += 1  # oversold = potential bounce
            elif rsi_val > 65:
                bearish_signals += 1  # overbought = potential reversal

            # 3. VWAP deviation
            total_signals += 1
            if vwap_deviation < -0.003:
                bullish_signals += 1  # below VWAP = undervalued
            elif vwap_deviation > 0.003:
                bearish_signals += 1  # above VWAP = overvalued

            # 4. Momentum projection
            total_signals += 1
            if momentum_slope > 0.02:
                bullish_signals += 1
            elif momentum_slope < -0.02:
                bearish_signals += 1

            # 5. Higher timeframe alignment
            total_signals += 1
            if ht_trend_up:
                bullish_signals += 1
            else:
                bearish_signals += 1

            # ── Score calculation ───────────────────────────────────────────
            bull_ratio = bullish_signals / total_signals if total_signals > 0 else 0.5
            bear_ratio = bearish_signals / total_signals if total_signals > 0 else 0.5

            # Base score: 0.5 neutral, >0.5 bullish, <0.5 bearish
            score = 0.5 + (bull_ratio - bear_ratio) * 0.4
            score = round(max(0.0, min(1.0, score)), 4)

            # Direction and confidence
            if score >= 0.62:
                direction = "buy"
                confidence = min(0.90, 0.55 + (score - 0.62) * 2.0)
                reason = (
                    f"forecast_bullish ema={'above' if ema_bullish else 'below'} "
                    f"rsi={rsi_val:.1f} vwap_dev={vwap_deviation:.3f} "
                    f"slope={momentum_slope:.3f} ht={'up' if ht_trend_up else 'down'}"
                )
            elif score <= 0.38:
                direction = "sell"
                confidence = min(0.90, 0.55 + (0.38 - score) * 2.0)
                reason = (
                    f"forecast_bearish ema={'above' if ema_bullish else 'below'} "
                    f"rsi={rsi_val:.1f} vwap_dev={vwap_deviation:.3f} "
                    f"slope={momentum_slope:.3f} ht={'up' if ht_trend_up else 'down'}"
                )
            else:
                direction = "hold"
                confidence = 0.40
                reason = (
                    f"forecast_neutral bull={bullish_signals}/{total_signals} "
                    f"rsi={rsi_val:.1f} slope={momentum_slope:.3f}"
                )

            signal = self.make_signal(
                symbol=symbol,
                score=score,
                direction=direction,
                confidence=confidence,
                reason=reason,
                metadata={
                    "ema9": round(ema9_now, 4),
                    "ema21": round(ema21_now, 4),
                    "ema_bullish": ema_bullish,
                    "ema_cross_up": ema_cross_up,
                    "ema_cross_down": ema_cross_down,
                    "rsi": round(rsi_val, 2),
                    "vwap": round(vwap_val, 4),
                    "vwap_deviation": round(vwap_deviation, 4),
                    "momentum_slope": round(momentum_slope, 4),
                    "ht_trend_up": ht_trend_up,
                    "bullish_signals": bullish_signals,
                    "bearish_signals": bearish_signals,
                    "total_signals": total_signals,
                },
            )
            return self.signal_to_dict(signal)

        except Exception:
            self.logger.exception("ForecastAgent failed for %s", symbol)
            return None