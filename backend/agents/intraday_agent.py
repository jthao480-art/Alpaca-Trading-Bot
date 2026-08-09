from __future__ import annotations

"""
intraday_agent.py — Combined intraday provisional signals agent.

Implements all 4 validated provisional signals from Tradetiq 5.0's
intraday_signal_engine.py, using EXACT production logic (thresholds,
regime filter, streak semantics, Wave's rel-vol calc).

Signals:
  1. Ripple provisional — yesterday's down-streak 2-4, extending today
  2. Ares Bullish provisional — exactly streak==2 yesterday, extending today
  3. Wave provisional ("Still-Down-Intraday+Volume") — down >=1% from open + volume >=1.3x
  4. Surge ("Momentum Continuation") — up >=1% from own open

All data pulled from Alpaca directly — no Tradetiq Postgres needed.

IMPORTANT: Do not alter thresholds/comparisons below unless you intend
to diverge from Tradetiq's validated research. Win rates were validated
against THESE EXACT conditions.

Enable via .env:
  USE_INTRADAY_AGENT=true
  USE_DEFAULT_AGENTS=true  (or false to run intraday only)
"""

import asyncio
import statistics
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars

ET = ZoneInfo("America/New_York")

# ── Checkpoint window ──────────────────────────────────────────────────────
# Only fire intraday signals between 12:30 PM and 3:00 PM ET
# (matches Tradetiq's ~1pm ET checkpoint logic)
_CHECKPOINT_START = time(12, 30)
_CHECKPOINT_END = time(15, 0)

# ── Regime filter ──────────────────────────────────────────────────────────
REGIME_SMA_WINDOW = 20
REGIME_LOOKBACK = 10

def get_regime_passed(spy_daily_closes: list[float]) -> bool | None:
    """
    True if SPY's 20-day SMA is HIGHER than it was 10 trading days ago.
    Returns None if insufficient data.
    """
    if len(spy_daily_closes) < REGIME_SMA_WINDOW + REGIME_LOOKBACK:
        return None
    current_sma = statistics.mean(spy_daily_closes[-REGIME_SMA_WINDOW:])
    prior_window = spy_daily_closes[-(REGIME_SMA_WINDOW + REGIME_LOOKBACK):-REGIME_LOOKBACK]
    prior_sma = statistics.mean(prior_window)
    return current_sma >= prior_sma

# ── Ripple provisional ─────────────────────────────────────────────────────
RIPPLE_STREAK_MIN = 2
RIPPLE_STREAK_MAX = 4

def check_ripple_provisional(
    regime_passed: bool,
    current_price: float,
    prior_close: float,
    yesterday_down_streak: int,
) -> str | None:
    provisional_down = (current_price - prior_close) / prior_close < 0
    if (
        regime_passed
        and provisional_down
        and RIPPLE_STREAK_MIN <= yesterday_down_streak <= RIPPLE_STREAK_MAX
    ):
        return "Bullish"
    return None

# ── Ares Bullish provisional ───────────────────────────────────────────────
def check_ares_bullish_provisional(
    regime_passed: bool,
    current_price: float,
    prior_close: float,
    yesterday_down_streak: int,
) -> str | None:
    provisional_down = (current_price - prior_close) / prior_close < 0
    if regime_passed and provisional_down and yesterday_down_streak == 2:
        return "Bullish"
    return None

# ── Wave provisional ───────────────────────────────────────────────────────
WAVE_MOMENTUM_THRESHOLD = -0.01   # down >= 1% from today's open
WAVE_REL_VOL_THRESHOLD = 1.3      # volume >= 1.3x trailing same-hour baseline
WAVE_REL_VOL_LOOKBACK_DAYS = 20

def get_wave_relative_volume(
    current_volume_so_far: float | None,
    trailing_same_hour_volumes: list[float],
    lookback_days: int = WAVE_REL_VOL_LOOKBACK_DAYS,
) -> float | None:
    if current_volume_so_far is None:
        return None
    if len(trailing_same_hour_volumes) < lookback_days:
        return None
    baseline_vals = [v for v in trailing_same_hour_volumes[-lookback_days:] if v]
    if len(baseline_vals) < lookback_days * 0.7:
        return None
    baseline_avg = statistics.mean(baseline_vals)
    if baseline_avg == 0:
        return None
    return current_volume_so_far / baseline_avg

def check_wave_provisional(
    day_open: float,
    current_price: float,
    relative_volume: float | None,
) -> str | None:
    if day_open is None or current_price is None or relative_volume is None:
        return None
    intraday_momentum = (current_price - day_open) / day_open
    if intraday_momentum <= WAVE_MOMENTUM_THRESHOLD and relative_volume >= WAVE_REL_VOL_THRESHOLD:
        return "Bullish"
    return None

# ── Surge ──────────────────────────────────────────────────────────────────
SURGE_MOMENTUM_THRESHOLD = 0.01  # up >= 1% from today's open

def check_surge(day_open: float, current_price: float) -> str | None:
    if day_open is None or current_price is None:
        return None
    surge_momentum = (current_price - day_open) / day_open
    if surge_momentum >= SURGE_MOMENTUM_THRESHOLD:
        return "Bullish"
    return None


# ── Volume baseline cache ──────────────────────────────────────────────────
# Stores per-symbol, per-hour volume readings to build Wave's self-owned
# baseline. Accumulates naturally over time — Wave will return no_data
# until ~20 trading days of history are built up per symbol/hour.
# In-memory only: resets on bot restart. For persistence, would need DB.
_volume_baseline: dict[str, list[float]] = {}  # key: "SYMBOL_HH"

def _baseline_key(symbol: str, hour: int) -> str:
    return f"{symbol}_{hour:02d}"

def _record_volume(symbol: str, hour: int, volume: float) -> None:
    key = _baseline_key(symbol, hour)
    if key not in _volume_baseline:
        _volume_baseline[key] = []
    _volume_baseline[key].append(volume)
    # Keep only last 30 days
    _volume_baseline[key] = _volume_baseline[key][-30:]

def _get_trailing_volumes(symbol: str, hour: int) -> list[float]:
    return _volume_baseline.get(_baseline_key(symbol, hour), [])


class IntradayAgent(BaseAgent):
    name = "intraday"

    # Class-level SPY cache — fetch once per cycle, shared across all symbols
    _spy_closes: list[float] = []
    _spy_cache_ts: float = 0.0
    _SPY_CACHE_TTL: float = 300.0  # 5 minutes

    async def _get_spy_closes(self) -> list[float]:
        import time as _time
        now = _time.time()
        if now - IntradayAgent._spy_cache_ts < IntradayAgent._SPY_CACHE_TTL and IntradayAgent._spy_closes:
            return IntradayAgent._spy_closes
        try:
            bars = await get_bars("SPY", timeframe="1Day", limit=35)
            if bars and len(bars) >= 30:
                IntradayAgent._spy_closes = [float(b.get("c", 0) or 0) for b in bars]
                IntradayAgent._spy_cache_ts = now
        except Exception:
            pass
        return IntradayAgent._spy_closes

    def _is_checkpoint_window(self) -> bool:
        """Only fire signals between 12:30 PM and 3:00 PM ET."""
        now = datetime.now(ET).time()
        return _CHECKPOINT_START <= now <= _CHECKPOINT_END

    async def analyze(self, symbol: str) -> Optional[dict[str, Any]]:
        try:
            # Only fire during checkpoint window
            if not self._is_checkpoint_window():
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="intraday: outside checkpoint window",
                    metadata={"intraday_active": False},
                )

            # Fetch daily bars (need 30+ for regime + streak)
            daily_bars = await get_bars(symbol, timeframe="1Day", limit=35)
            if not daily_bars or len(daily_bars) < 5:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="intraday: insufficient daily bars",
                    metadata={"intraday_active": False},
                )

            closes = [float(b.get("c", 0) or 0) for b in daily_bars]
            today_open = float(daily_bars[-1].get("o", 0) or 0)
            prior_close = closes[-2] if len(closes) >= 2 else 0.0
            today_volume = float(daily_bars[-1].get("v", 0) or 0)

            # Current price from latest intraday bar
            intraday_bars = await get_bars(symbol, timeframe="5Min", limit=5)
            current_price = float(intraday_bars[-1].get("c", 0) or 0) if intraday_bars else closes[-1]
            if current_price <= 0:
                current_price = closes[-1]

            # Compute yesterday's down-streak
            yesterday_down_streak = 0
            for i in range(len(closes) - 2, 0, -1):
                if closes[i] < closes[i - 1]:
                    yesterday_down_streak += 1
                else:
                    break

            # Regime filter (SPY 20-day SMA rising)
            spy_closes = await self._get_spy_closes()
            regime_passed = get_regime_passed(spy_closes)
            if regime_passed is None:
                regime_passed = True  # default to True if insufficient SPY data

            # Record volume for Wave baseline
            current_hour = datetime.now(ET).hour
            if today_volume > 0:
                _record_volume(symbol, current_hour, today_volume)

            # Wave relative volume
            trailing_vols = _get_trailing_volumes(symbol, current_hour)
            rel_vol = get_wave_relative_volume(today_volume, trailing_vols)

            # ── Run all 4 checks ──
            ripple = check_ripple_provisional(
                regime_passed=regime_passed,
                current_price=current_price,
                prior_close=prior_close,
                yesterday_down_streak=yesterday_down_streak,
            )
            ares = check_ares_bullish_provisional(
                regime_passed=regime_passed,
                current_price=current_price,
                prior_close=prior_close,
                yesterday_down_streak=yesterday_down_streak,
            )
            wave = check_wave_provisional(
                day_open=today_open,
                current_price=current_price,
                relative_volume=rel_vol,
            )
            surge = check_surge(
                day_open=today_open,
                current_price=current_price,
            )

            intraday_pct = (current_price - today_open) / today_open if today_open > 0 else 0.0
            prior_pct = (current_price - prior_close) / prior_close if prior_close > 0 else 0.0

            metadata = {
                "intraday_active": bool(ripple or ares or wave or surge),
                "ripple_provisional": ripple,
                "ares_bullish_provisional": ares,
                "wave_provisional": wave,
                "surge": surge,
                "regime_passed": regime_passed,
                "yesterday_down_streak": yesterday_down_streak,
                "intraday_pct": round(intraday_pct, 4),
                "prior_pct": round(prior_pct, 4),
                "today_open": today_open,
                "current_price": current_price,
                "relative_volume": round(rel_vol, 3) if rel_vol is not None else None,
                "wave_baseline_days": len(trailing_vols),
            }

            # Priority: Ripple > Ares > Wave > Surge
            # All are bullish buy signals
            if ripple:
                return self.make_signal(
                    symbol=symbol,
                    score=0.78,
                    direction="buy",
                    confidence=0.70,
                    reason=f"Ripple provisional: {yesterday_down_streak}-day streak extending today",
                    metadata=metadata,
                )
            elif ares:
                return self.make_signal(
                    symbol=symbol,
                    score=0.75,
                    direction="buy",
                    confidence=0.68,
                    reason=f"Ares Bullish provisional: streak==2 extending to 3 today",
                    metadata=metadata,
                )
            elif wave:
                return self.make_signal(
                    symbol=symbol,
                    score=0.76,
                    direction="buy",
                    confidence=0.69,
                    reason=f"Wave provisional: down {intraday_pct:.1%} from open, vol={rel_vol:.2f}x",
                    metadata=metadata,
                )
            elif surge:
                return self.make_signal(
                    symbol=symbol,
                    score=0.74,
                    direction="buy",
                    confidence=0.67,
                    reason=f"Surge: up {intraday_pct:.1%} from open",
                    metadata=metadata,
                )
            else:
                return self.make_signal(
                    symbol=symbol,
                    score=0.5,
                    direction="hold",
                    confidence=0.1,
                    reason="intraday: no provisional signals firing",
                    metadata=metadata,
                )

        except Exception:
            self.logger.exception("IntradayAgent failed for %s", symbol)
            return self.make_signal(
                symbol=symbol,
                score=0.5,
                direction="hold",
                confidence=0.0,
                reason="intraday_agent_error",
                metadata={"intraday_active": False},
            )