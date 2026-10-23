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

Each of the 4 checks above can also be individually toggled (all default
true — an untouched deployment runs all four exactly as before):
  USE_RIPPLE=true/false             (check 1: Ripple provisional)
  USE_ARES_PROVISIONAL=true/false   (check 2: Ares Bullish provisional)
  USE_WAVE_PROVISIONAL=true/false   (check 3: Wave provisional — local,
                                      computed here from Alpaca bars; distinct
                                      from Tradetiq's own API-sourced Wave
                                      provisional signal in tradetiq_agent.py,
                                      and distinct from the separate standalone
                                      WaveAgent controlled by USE_WAVE_AGENT)
  USE_SURGE=true/false              (check 4: Surge)
"""

import asyncio
import logging
import os
import statistics
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any, Optional

from backend.agents.base import BaseAgent
from backend.services.bars_service import get_bars_cached as get_bars
from backend import config

ET = ZoneInfo("America/New_York")

# ── Checkpoint window ──────────────────────────────────────────────────────
# Fire intraday signals from market open (9:30 AM ET) through 3:00 PM ET.
# Originally started at 12:30 PM to match Tradetiq's own checkpoint logic,
# but pre-market/after-market price action already moves these tickers by
# the open, so waiting until midday missed real setups — moved to 9:30.
_CHECKPOINT_START = time(9, 30)
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
SURGE_MOMENTUM_THRESHOLD = 0.02  # up >= 2% from today's open

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


# ── Wave relative volume (rebuilt) ─────────────────────────────────────────
# The original baseline was an in-memory list that appended today's running
# volume on every scan, so (a) it was wiped by every restart/redeploy and
# (b) its "20 days" were really 20 scans, mostly from the same day, with the
# current reading included in its own average — so Wave either never fired or
# compared today against itself. This computes the real thing straight from
# Alpaca history, so nothing needs to accumulate and restarts don't matter:
# today's cumulative volume since the open, divided by the average of the
# previous sessions' cumulative volume over the SAME time-of-day window.
# Thresholds (down >=1% from open, rel-vol >=1.3x, 20-day lookback) are
# unchanged. It only runs for stocks already down >=1% from today's open,
# which is the only case Wave could fire anyway, to keep data calls low.
_WAVE_HIST_CACHE: dict[tuple, tuple[Optional[float], int]] = {}
_WAVE_MIN_PRIOR_SESSIONS = 14  # >= 70% of the 20-day lookback


async def _wave_relative_volume_from_history(symbol: str) -> tuple[Optional[float], int]:
    """Returns (relative_volume or None, number_of_prior_sessions_used)."""
    now_et = datetime.now(ET)
    cut_min = (now_et.hour * 60 + now_et.minute) // 30 * 30   # completed 30-min bars only
    key = (symbol, now_et.date().isoformat(), cut_min)
    if key in _WAVE_HIST_CACHE:
        return _WAVE_HIST_CACHE[key]
    if len(_WAVE_HIST_CACHE) > 20000:
        _WAVE_HIST_CACHE.clear()
    result: tuple[Optional[float], int] = (None, 0)
    try:
        from backend.services.bars_service import _service, _default_window
        start, end = _default_window(35)
        payload = await _service.get_bars(symbol, start, end, timeframe="30Min", limit=1000)
        bars = (payload.get("bars") or []) if isinstance(payload, dict) else []
        open_min = 9 * 60 + 30
        per_day: dict[str, float] = {}
        for b in bars:
            try:
                ts = datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00")).astimezone(ET)
                vol = float(b.get("v") or 0)
            except Exception:
                continue
            m = ts.hour * 60 + ts.minute
            if m < open_min or m >= cut_min:
                continue
            d = ts.date().isoformat()
            per_day[d] = per_day.get(d, 0.0) + vol
        today = now_et.date().isoformat()
        today_cum = per_day.pop(today, 0.0)
        prior = [per_day[d] for d in sorted(per_day)][-WAVE_REL_VOL_LOOKBACK_DAYS:]
        if today_cum > 0 and len(prior) >= _WAVE_MIN_PRIOR_SESSIONS:
            result = (get_wave_relative_volume(today_cum, prior, lookback_days=len(prior)), len(prior))
        else:
            result = (None, len(prior))
    except Exception:
        logging.getLogger(__name__).exception("wave rel-vol history failed for %s", symbol)
    _WAVE_HIST_CACHE[key] = result
    return result



# ── Wave relative volume: zero per-symbol calls ("curve" mode, default) ──────
# The agent already fetches every symbol's 35 daily bars (the last one is today's
# volume so far). Rather than pulling 35 days of 30-min bars per symbol (thousands
# of extra requests per pass, refetched every half hour), compare today's volume to
# that symbol's 20-day average DAILY volume scaled by how much of a normal day's
# volume has typically traded by now. The "typical" intraday curve comes from a few
# very liquid reference ETFs, fetched once per ET day (<= 5 requests/day total).
# WAVE_REL_VOL_MODE=history restores the exact per-symbol 30-min method above.
_WAVE_CURVE_REFS = ("SPY", "QQQ", "IWM", "DIA", "XLF")
# cumulative share of the day's volume after k completed 30-min bars (k = 0..13);
# only used if the live curve can't be built.
_WAVE_DEFAULT_CURVE = [0.0, 0.14, 0.22, 0.28, 0.33, 0.38, 0.43, 0.48, 0.53, 0.58, 0.64, 0.71, 0.80, 1.0]
_WAVE_CURVE_CACHE: dict[str, list[float]] = {}


async def _wave_curve() -> list[float]:
    today = datetime.now(ET).date().isoformat()
    cached = _WAVE_CURVE_CACHE.get(today)
    if cached:
        return cached
    curve = list(_WAVE_DEFAULT_CURVE)
    try:
        from backend.services.bars_service import _service, _default_window
        start, end = _default_window(35)
        open_min = 9 * 60 + 30
        fracs: list[list[float]] = []
        for ref in _WAVE_CURVE_REFS:
            payload = await _service.get_bars(ref, start, end, timeframe="30Min", limit=1000)
            bars = (payload.get("bars") or []) if isinstance(payload, dict) else []
            per_day: dict[str, list[float]] = {}
            for b in bars:
                try:
                    ts = datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00")).astimezone(ET)
                    vol = float(b.get("v") or 0)
                except Exception:
                    continue
                m = ts.hour * 60 + ts.minute
                k = (m - open_min) // 30
                if k < 0 or k > 12:
                    continue
                per_day.setdefault(ts.date().isoformat(), [0.0] * 13)[int(k)] += vol
            for d, slots in per_day.items():
                if d == today:
                    continue
                total = sum(slots)
                if total > 0 and all(s > 0 for s in slots):   # full session only
                    run, cum = 0.0, [0.0]
                    for s in slots:
                        run += s
                        cum.append(run / total)
                    fracs.append(cum)
        if len(fracs) >= 10:
            curve = [statistics.mean(f[i] for f in fracs) for i in range(14)]
            curve[13] = 1.0
    except Exception:
        logging.getLogger(__name__).exception("wave volume curve build failed; using default curve")
    _WAVE_CURVE_CACHE.clear()
    _WAVE_CURVE_CACHE[today] = curve
    return curve


def _curve_fraction(curve: list[float], now_et: datetime) -> float:
    """Typical share of the day's volume traded by now (interpolated inside the 30-min slot)."""
    minutes = (now_et.hour * 60 + now_et.minute + now_et.second / 60.0) - (9 * 60 + 30)
    if minutes <= 0:
        return 0.0
    if minutes >= 390:
        return 1.0
    k = int(minutes // 30)
    within = (minutes - k * 30) / 30.0
    return curve[k] + (curve[k + 1] - curve[k]) * within


_PRIOR_VOL_CACHE: dict[tuple, list[float]] = {}


async def _prior_daily_volumes(symbol: str, today_iso: str) -> list[float]:
    """Prior sessions' daily volumes (oldest -> newest, today excluded), fetched once per
    symbol per ET day. The shared daily-bar fetch only reaches back 14 calendar days
    (~10 sessions), which is fewer than the 14 Wave needs, so in curve mode Wave's
    relative volume was always None. Only called for stocks already down >=1% from the
    open (a few dozen a day), so this adds very few data requests."""
    key = (symbol, today_iso)
    if key in _PRIOR_VOL_CACHE:
        return _PRIOR_VOL_CACHE[key]
    if len(_PRIOR_VOL_CACHE) > 5000:
        _PRIOR_VOL_CACHE.clear()
    vols: list[float] = []
    try:
        from backend.services.bars_service import _service, _default_window
        start, end = _default_window(int(os.getenv("WAVE_PRIOR_DAYS", "45")))
        payload = await _service.get_bars(symbol, start, end, timeframe="1Day", limit=60)
        bars = (payload.get("bars") or []) if isinstance(payload, dict) else []
        rows = []
        for b in bars:
            try:
                d = datetime.fromisoformat(str(b.get("t")).replace("Z", "+00:00")).astimezone(ET).date().isoformat()
                rows.append((d, float(b.get("v") or 0)))
            except Exception:
                continue
        rows.sort()
        vols = [v for d, v in rows if d != today_iso]
    except Exception:
        # do not cache a failed fetch (e.g. a 429): let the next pass retry it
        logging.getLogger(__name__).warning("wave prior-volume fetch failed for %s", symbol)
        return []
    _PRIOR_VOL_CACHE[key] = vols
    return vols


async def _wave_relative_volume_fast(daily_bars: list[dict], symbol: Optional[str] = None) -> tuple[Optional[float], int]:
    """(relative_volume or None, prior_sessions_used) from the daily bars already in hand."""
    now_et = datetime.now(ET)
    try:
        last = daily_bars[-1]
        last_day = datetime.fromisoformat(str(last.get("t")).replace("Z", "+00:00")).astimezone(ET).date()
        if last_day != now_et.date():
            return None, 0                      # no trade yet today -> stale bar
        today_vol = float(last.get("v") or 0)
        prior = [float(b.get("v") or 0) for b in daily_bars[:-1]][-WAVE_REL_VOL_LOOKBACK_DAYS:]
        if symbol and len([v for v in prior if v]) < _WAVE_MIN_PRIOR_SESSIONS:
            deeper = await _prior_daily_volumes(symbol, now_et.date().isoformat())
            if len([v for v in deeper if v]) > len([v for v in prior if v]):
                prior = deeper[-WAVE_REL_VOL_LOOKBACK_DAYS:]
        frac = _curve_fraction(await _wave_curve(), now_et)
        if today_vol <= 0 or frac < 0.05 or len([v for v in prior if v]) < _WAVE_MIN_PRIOR_SESSIONS:
            return None, len(prior)
        projected = today_vol / frac            # full-day volume at today's pace
        return get_wave_relative_volume(projected, prior, lookback_days=len(prior)), len(prior)
    except Exception:
        logging.getLogger(__name__).exception("wave fast rel-vol failed")
        return None, 0


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
        """Only fire signals between market open (9:30 AM ET) and 3:00 PM ET."""
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

            # Wave relative volume: real same-time-of-day baseline from Alpaca
            # history (see _wave_relative_volume_from_history). Only computed
            # for stocks already down >= WAVE_MOMENTUM_THRESHOLD from the open.
            rel_vol = None
            wave_baseline_days = 0
            if (
                getattr(config, "USE_WAVE_PROVISIONAL", True)
                and today_open > 0
                and (current_price - today_open) / today_open <= WAVE_MOMENTUM_THRESHOLD
            ):
                if os.getenv("WAVE_REL_VOL_MODE", "curve").strip().lower() == "history":
                    rel_vol, wave_baseline_days = await _wave_relative_volume_from_history(symbol)
                else:
                    rel_vol, wave_baseline_days = await _wave_relative_volume_fast(daily_bars, symbol)

            # ── Run all 4 checks — each individually toggleable via Railway
            # Variables (USE_RIPPLE / USE_ARES_PROVISIONAL / USE_WAVE_PROVISIONAL /
            # USE_SURGE), independent of USE_WAVE_AGENT (a separate, unrelated
            # standalone WaveAgent) and independent of Tradetiq's own API-sourced
            # wave_provisional signals (tradetiq_agent.py / USE_TRADETIQ_WAVE_PROVISIONAL).
            # All four default to True, so an untouched deployment runs all four
            # exactly as before. ──
            ripple = check_ripple_provisional(
                regime_passed=regime_passed,
                current_price=current_price,
                prior_close=prior_close,
                yesterday_down_streak=yesterday_down_streak,
            ) if getattr(config, "USE_RIPPLE", True) else None
            ares = check_ares_bullish_provisional(
                regime_passed=regime_passed,
                current_price=current_price,
                prior_close=prior_close,
                yesterday_down_streak=yesterday_down_streak,
            ) if getattr(config, "USE_ARES_PROVISIONAL", True) else None
            wave = check_wave_provisional(
                day_open=today_open,
                current_price=current_price,
                relative_volume=rel_vol,
            ) if getattr(config, "USE_WAVE_PROVISIONAL", True) else None
            surge = check_surge(
                day_open=today_open,
                current_price=current_price,
            ) if getattr(config, "USE_SURGE", True) else None

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
                "wave_baseline_days": wave_baseline_days,
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