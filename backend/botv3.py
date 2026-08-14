from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from collections.abc import Iterable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from backend import config
from backend.agents.news_agent import NewsAgent
from backend.agents.momentum_agent import MomentumAgent
from backend.agents.volume_agent import VolumeAgent
from backend.agents.forecast_agent import ForecastAgent
from backend.agents.fundamentals_agent import FundamentalsAgent
from backend.agents.intraday_agent import IntradayAgent
from backend.agents.wallet_agent import WalletAgent
from backend.agents.insideragent import InsiderAgent
from backend.agents.wave_agent import WaveAgent
from backend.agents.ares_agent import AresAgent
from backend.agents.social_agent import SocialAgent
from backend.execution import (
    get_latest_price,
    place_bracket_buy as execution_place_bracket_buy,
    place_bracket_short as execution_place_bracket_short,
    place_market_sell,
    place_trailing_stop_sell,
    place_trailing_stop_buy,
    _get_open_orders_for_symbol,
    _get_open_positions,
    _cancel_order_by_id,
)
from backend.services.bars_service import get_bars
from backend.services.account_service import get_account_buying_power
from backend.services.trade_ledger_service import (
    add_entry,
    close_entry,
    is_in_cooldown,
    load_ledger,
    save_ledger,
    _from_iso,
)


logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_PENDING_SELLS: set[str] = set()
_PENDING_BUYS: set[str] = set()
_BOUGHT_THIS_SESSION: set[str] = set()
_LAST_LOSER_SWEEP: datetime | None = None

MAX_POSITIONS = int(getattr(config, "MAX_POSITIONS", 150))
DAILY_LOSS_LIMIT = float(getattr(config, "DAILY_LOSS_LIMIT", -2000))
MARKET_FILTER_THRESHOLD = float(getattr(config, "MARKET_FILTER_THRESHOLD", -0.005))
HARD_STOP_PCT = float(getattr(config, "HARD_STOP_PCT", 0.94))
TRAIL_PCT = float(getattr(config, "TRAIL_PCT", 4.0))
LOSER_EXIT_THRESHOLD = float(getattr(config, "LOSER_EXIT_THRESHOLD", -0.05))
HALT_ENTRIES = False

_DATA_DIR = pathlib.Path(getattr(config, "DATA_DIR", "."))
_BLACKLIST_FILE = _DATA_DIR / "blacklist.json"
_BLACKLIST_TTL_DAYS = 15

# Bond ETFs and low-vol ETFs that should never be shorted
_SHORT_BLACKLIST = {
    'TLT', 'IEF', 'SHY', 'BND', 'AGG', 'LQD', 'HYG', 'JNK',
    'VCIT', 'VCSH', 'VCLT', 'BSV', 'BIV', 'BLV', 'VCRB',
    'TBIL', 'SHV', 'SGOV', 'SCHO', 'SCHR', 'SCHZ',
    'SPIP', 'STIP', 'TIP', 'TIPX',
    'SJNK', 'ANGL', 'HYLS', 'PGHY', 'PCY',
    'IBDS', 'IBDT', 'IBDY', 'IBDZ',
    'VMBS', 'MBB', 'GNMA',
    'TOTL', 'FNDX', 'FXNAX',
    'UUP', 'FXE', 'FXY', 'FXB',
    'GLD', 'SLV', 'IAU', 'GLDM',
    'VIX', 'VIXY', 'UVXY',
}


def _load_blacklist() -> set:
    base = {
        "POEL", "BEEP", "SAFX", "CBUS", "ALGS", "AMOD", "RXT",
        "TGEN", "SHMD", "ARBE", "RDCM", "OUST", "FDMT", "CTM", "CYPH",
    }
    if not _BLACKLIST_FILE.exists():
        return base
    try:
        data = json.loads(_BLACKLIST_FILE.read_text())
        now = datetime.utcnow()
        cutoff = now - timedelta(days=_BLACKLIST_TTL_DAYS)
        if isinstance(data, list):
            active = {s: now.isoformat() for s in data}
        elif isinstance(data, dict):
            active = {
                s: ts for s, ts in data.items()
                if datetime.fromisoformat(ts) > cutoff
            }
        else:
            active = {}
        _BLACKLIST_FILE.write_text(json.dumps(active, indent=2))
        expired = set(data.keys() if isinstance(data, dict) else data) - set(active.keys())
        if expired:
            logger.info("Blacklist cleanup — expired %d symbols: %s", len(expired), sorted(expired))
        return base | set(active.keys())
    except Exception:
        logger.exception("Failed to load blacklist")
        return base


def _save_blacklist() -> None:
    base = {
        "POEL", "BEEP", "SAFX", "CBUS", "ALGS", "AMOD", "RXT",
        "TGEN", "SHMD", "ARBE", "RDCM", "OUST", "FDMT", "CTM", "CYPH",
    }
    try:
        existing = {}
        if _BLACKLIST_FILE.exists():
            try:
                data = json.loads(_BLACKLIST_FILE.read_text())
                if isinstance(data, dict):
                    existing = data
            except Exception:
                pass
        now = datetime.utcnow().isoformat()
        updated = {**existing}
        for symbol in BLACKLIST - base:
            if symbol not in updated:
                updated[symbol] = now
        _BLACKLIST_FILE.write_text(json.dumps(updated, indent=2))
    except Exception:
        logger.exception("Failed to save blacklist")


def maybe_blacklist(symbol: str, return_pct: float) -> None:
    if return_pct < LOSER_EXIT_THRESHOLD:
        BLACKLIST.add(symbol)
        _save_blacklist()
        logger.info(
            "Blacklisted %s after %.1f%% loss — expires in %d days",
            symbol, return_pct * 100, _BLACKLIST_TTL_DAYS,
        )


BLACKLIST: set = _load_blacklist()


def _cfg_float(name: str, default: float) -> float:
    return float(getattr(config, name, default) or default)


def _cfg_int(name: str, default: int) -> int:
    return int(getattr(config, name, default) or default)


def _cfg_any_float(*names: str, default: float = 0.0) -> float:
    for name in names:
        value = getattr(config, name, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                continue
    return float(default)


def compute_momentum_score(signal: dict[str, Any]) -> float:
    metadata = signal.get("metadata", {}) or {}
    base_score = float(signal.get("score", 0.0) or 0.0)
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    volume_ratio = float(metadata.get("volume_ratio", 1.0) or 1.0)
    volume_acceleration = float(metadata.get("volume_acceleration", 1.0) or 1.0)
    velocity_score = float(metadata.get("velocity_score", 0.25) or 0.25)
    news_spike = bool(metadata.get("news_spike", False))
    spike_bonus = 0.10 if news_spike else 0.0
    social_proxy = min(1.0, velocity_score + spike_bonus)
    news_confidence = float(metadata.get("news_confidence", 0.0) or 0.0)
    insider_adj = float(metadata.get("insider_adjustment", 0.0) or 0.0)
    insider_score = min(1.0, max(0.0, insider_adj + 0.5))
    vol_normalized = min(1.0, max(0.0, (volume_ratio - 1.0) / 3.0))
    score = (
        0.30 * social_proxy
        + 0.20 * news_confidence
        + 0.20 * vol_normalized
        + 0.10 * insider_score
        + 0.10 * base_score
        + 0.10 * confidence
    )
    return round(min(1.0, max(0.0, score)), 4)


def compute_short_score(signal: dict[str, Any]) -> float:
    metadata = signal.get("metadata", {}) or {}
    base_score = float(signal.get("score", 0.0) or 0.0)
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    volume_ratio = float(metadata.get("volume_ratio", 1.0) or 1.0)
    volume_acceleration = float(metadata.get("volume_acceleration", 1.0) or 1.0)
    vol_fade = min(1.0, max(0.0, (1.0 - volume_ratio) / 0.5))
    accel_fade = min(1.0, max(0.0, (1.0 - volume_acceleration) / 0.5))
    news_confidence = float(metadata.get("news_confidence", 0.0) or 0.0)
    news_label = str(metadata.get("news_label", "neutral")).lower()
    news_bearish = news_confidence if news_label == "negative" else 0.0
    score = (
        0.35 * base_score
        + 0.25 * confidence
        + 0.20 * vol_fade
        + 0.10 * accel_fade
        + 0.10 * news_bearish
    )
    return round(min(1.0, max(0.0, score)), 4)


def trade_priority(signal: dict[str, Any]) -> float:
    metadata = signal.get("metadata", {}) or {}
    momentum_score = compute_momentum_score(signal)
    score = float(signal.get("score", 0.0) or 0.0)
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    volume_ratio = float(metadata.get("volume_ratio", 0.0) or 0.0)
    volume_acceleration = float(metadata.get("volume_acceleration", 0.0) or 0.0)
    breakout = 1.0 if bool(metadata.get("breakout", False)) else 0.0
    news_strength = float(metadata.get("news_strength", 0.0) or 0.0)
    return (
        0.40 * momentum_score
        + 0.30 * confidence
        + 0.20 * score
        + 0.05 * max(0.0, volume_ratio - 1.0)
        + 0.03 * max(0.0, volume_acceleration - 1.0)
        + 0.02 * breakout
        + 0.02 * news_strength
    )


def size_multiplier(momentum_score: float) -> float:
    if momentum_score < 0.35:
        return 0.5
    if momentum_score < 0.60:
        return 0.8
    if momentum_score < 0.95:
        return 1.0
    if momentum_score < 1.30:
        return 1.35
    return 1.70


def build_exit_plan(signal: dict[str, Any], bars_held: int = 0, green_gain_pct: float = 0.0) -> dict[str, float | bool]:
    momentum_score = compute_momentum_score(signal)
    if momentum_score < 0.35:
        tp, sl = 0.008, 0.005
    elif momentum_score < 0.60:
        tp, sl = 0.012, 0.007
    elif momentum_score < 0.95:
        tp, sl = 0.018, 0.010
    elif momentum_score < 1.30:
        tp, sl = 0.025, 0.012
    else:
        tp, sl = 0.035, 0.015
    tighten_after_bars = _cfg_int("EXIT_TIGHTEN_AFTER_BARS", 3)
    tight_loss_stop_pct = _cfg_float("TIGHT_LOSS_STOP_PCT", 0.004)
    trailing_stop_default = _cfg_float("TRAILING_STOP_PCT", 4.0)
    if bars_held >= tighten_after_bars and green_gain_pct <= 0.0:
        sl = min(sl, tight_loss_stop_pct)
    return {
        "take_profit_pct": tp,
        "stop_loss_pct": sl,
        "use_trailing": True,
        "trailing_stop_pct": trailing_stop_default,
    }


def load_all_tradable_symbols(trading_client: Any) -> list[str]:
    if trading_client is None:
        return list(getattr(config, "SYMBOL_UNIVERSE", []))
    try:
        req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = trading_client.get_all_assets(req)
    except Exception:
        logger.exception("failed to load alpaca assets")
        return list(getattr(config, "SYMBOL_UNIVERSE", []))
    symbols: list[str] = []
    for asset in assets:
        try:
            symbol = getattr(asset, "symbol", None)
            if not symbol:
                continue
            if not getattr(asset, "active", False):
                continue
            if not getattr(asset, "tradable", False):
                continue
            if str(getattr(asset, "class", "")).lower() != "us_equity":
                continue
            symbols.append(symbol)
        except Exception:
            continue
    return sorted(set(symbols))


def already_have_position(trading_client: Any, symbol: str) -> bool:
    try:
        trading_client.get_open_position(symbol)
        return True
    except Exception:
        return False


def under_position_limit(trading_client: Any) -> bool:
    try:
        positions = trading_client.get_all_positions()
        if len(positions) >= MAX_POSITIONS:
            logger.info("Position cap reached: %s/%s", len(positions), MAX_POSITIONS)
            return False
        return True
    except Exception:
        return True


def check_daily_loss_limit(trading_client: Any) -> bool:
    try:
        account = trading_client.get_account()
        today_pl = float(account.equity) - float(account.last_equity)
        if today_pl <= DAILY_LOSS_LIMIT:
            logger.warning("DAILY LOSS LIMIT HIT: %.2f — halting all entries", today_pl)
            return True
        return False
    except Exception:
        return False


def market_is_bullish(data_client: Any) -> bool:
    try:
        if data_client is None:
            return True
        bars = data_client.get_stock_bars("SPY", "1Min", limit=30).df
        if bars is None or bars.empty:
            return True
        spy_change = (bars["close"].iloc[-1] - bars["close"].iloc[0]) / bars["close"].iloc[0]
        if spy_change < MARKET_FILTER_THRESHOLD:
            logger.info("Market filter: SPY %.2f%% — skipping entries", spy_change * 100)
            return False
        return True
    except Exception:
        logger.exception("market filter failed")
        return True


def close_losing_positions(trading_client: Any, threshold_pct: float = LOSER_EXIT_THRESHOLD) -> None:
    try:
        positions = trading_client.get_all_positions()
        losers = sorted(
            [p for p in positions if float(p.unrealized_plpc) < threshold_pct],
            key=lambda p: float(p.unrealized_plpc),
        )
        for p in losers:
            if p.symbol in _PENDING_SELLS:
                continue
            logger.info("Closing loser: %s %.1f%%", p.symbol, float(p.unrealized_plpc) * 100)
            _PENDING_SELLS.add(p.symbol)
            try:
                place_market_sell(p.symbol, abs(float(p.qty)))
                maybe_blacklist(p.symbol, float(p.unrealized_plpc))
            finally:
                _PENDING_SELLS.discard(p.symbol)
    except Exception:
        logger.exception("loser sweep failed")


class botV3:
    def __init__(
        self,
        symbols: Optional[Iterable[str]] = None,
        trading_client: Any | None = None,
        use_news: bool = True,
        use_volume: bool = True,
        use_momentum: bool = True,
        use_forecast: bool = True,
        use_fundamentals: bool = True,
        use_wallet: bool = True,
        use_insider: bool = True,
        max_concurrent_symbols: int = 50,
        batch_size: int = 50,
        cooldown_minutes: int = 20,
        buy_power_cap: float = 0.20,
        early_entry_threshold: float = 0.55,
        volume_ratio_entry: float = 1.15,
        volume_ratio_exit: float = 0.95,
    ) -> None:
        self.trading_client = trading_client
        self.symbols = list(symbols) if symbols is not None else load_all_tradable_symbols(trading_client)
        self.use_news = use_news
        self.use_volume = use_volume
        self.use_momentum = use_momentum
        self.use_forecast = use_forecast
        self.use_fundamentals = use_fundamentals
        self.use_wallet = use_wallet
        self.use_insider = use_insider
        self.max_concurrent_symbols = max(1, int(max_concurrent_symbols))
        self.batch_size = max(1, int(batch_size))
        self.cooldown_minutes = max(1, int(cooldown_minutes))
        self.buy_power_cap = max(0.01, min(float(buy_power_cap), 1.0))
        self.early_entry_threshold = max(0.0, min(float(early_entry_threshold), 1.0))
        self.volume_ratio_entry = max(0.0, float(volume_ratio_entry))
        self.volume_ratio_exit = max(0.0, float(volume_ratio_exit))
        self.agents: list[Any] = []

        self.exit_tighten_after_bars = _cfg_int("EXIT_TIGHTEN_AFTER_BARS", 3)
        self.tight_loss_stop_pct = _cfg_float("TIGHT_LOSS_STOP_PCT", 0.004)
        self.trailing_trigger_pct = _cfg_float("TRAILING_TRIGGER_PCT", 0.008)
        self.trailing_stop_pct = _cfg_float("TRAILING_STOP_PCT", 4.0)
        self.market_filter_threshold = _cfg_float("MARKET_FILTER_THRESHOLD", -0.005)
        self.market_filter_enabled = bool(getattr(config, "MARKET_FILTER_ENABLED", False))
        self.daily_position_cap = _cfg_int("DAILY_POSITION_CAP", MAX_POSITIONS)
        self.time_exit_minutes = _cfg_int("TIME_EXIT_MINUTES", 90)
        self.session_flatten_time = getattr(config, "SESSION_FLATTEN_TIME", "15:45")
        self.vix_threshold = _cfg_float("VIX_THRESHOLD", 25.0)
        self.vix_size_multiplier = _cfg_float("VIX_SIZE_MULTIPLIER", 0.5)
        self.order_size_multiplier = 1.0
        self._open_positions_cache: dict[str, float] | None = None
        self._bars_cache: dict[Any, Any] = {}
        self._news_cache: dict[Any, Any] = {}
        self._last_loser_sweep: datetime | None = None
        self._session_date: Any = None

        use_wave = bool(getattr(config, "USE_WAVE_AGENT", False))
        use_default = bool(getattr(config, "USE_DEFAULT_AGENTS", True))

        if use_default:
            if self.use_news:
                self.agents.append(NewsAgent())
            if self.use_volume:
                self.agents.append(VolumeAgent())
            if self.use_momentum:
                self.agents.append(MomentumAgent())
            if self.use_forecast:
                self.agents.append(ForecastAgent())
            if self.use_fundamentals:
                self.agents.append(FundamentalsAgent())
            if self.use_wallet:
                self.agents.append(WalletAgent())
            if self.use_insider:
                self.agents.append(InsiderAgent())
            self.agents.append(SocialAgent())

        if use_wave:
            self.agents.append(WaveAgent())
            logger.info("WaveAgent enabled")

        use_ares = bool(getattr(config, "USE_ARES_AGENT", False))
        if use_ares:
            self.agents.append(AresAgent())
            logger.info("AresAgent enabled")    

        use_intraday = bool(getattr(config, "USE_INTRADAY_AGENT", False))
        if use_intraday:
            self.agents.append(IntradayAgent())
            logger.info("IntradayAgent enabled (Ripple/Ares/Wave/Surge)")

    def _reset_cycle_cache(self) -> None:
        self._open_positions_cache = None
        self._bars_cache = {}
        self._news_cache = {}

    def _load_open_positions(self) -> dict[str, float]:
        if self._open_positions_cache is not None:
            return self._open_positions_cache
        if self.trading_client is None:
            self._open_positions_cache = {}
            return {}
        positions: dict[str, float] = {}
        try:
            raw_positions = self.trading_client.get_all_positions()
            for p in raw_positions:
                try:
                    positions[p.symbol] = float(p.qty or 0)
                except Exception:
                    continue
        except Exception:
            logger.exception("failed to load open positions")
        self._open_positions_cache = positions
        return positions

    def _load_position_objects(self) -> list[Any]:
        if self.trading_client is None:
            return []
        try:
            return list(self.trading_client.get_all_positions())
        except Exception:
            logger.exception("failed to fetch position objects")
            return []

    def _position_unrealized_plpc(self, position: Any) -> float | None:
        try:
            value = getattr(position, "unrealized_plpc", None)
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _position_unrealized_pl(self, position: Any) -> float | None:
        try:
            value = getattr(position, "unrealized_pl", None)
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    async def _liquidate_loser_sweep(self, ledger: Any) -> None:
        # Rate limit: only run once every 5 minutes to prevent SELL 0 spam
        global _LAST_LOSER_SWEEP
        _now = datetime.now(ET)
        if _LAST_LOSER_SWEEP is not None:
            elapsed = (_now - _LAST_LOSER_SWEEP).total_seconds()
            if elapsed < 300:
                logger.debug("loser_sweep: skipping — ran %.0fs ago", elapsed)
                return
        _LAST_LOSER_SWEEP = _now

        positions = self._load_position_objects()
        if not positions:
            logger.debug("loser_sweep: no open positions")
            return

        candidates: list[tuple[float, Any]] = []
        for p in positions:
            symbol = getattr(p, "symbol", "")
            qty = float(getattr(p, "qty", 0) or 0)
            plpc = self._position_unrealized_plpc(p)
            if not symbol or qty <= 0:
                continue
            if plpc is None:
                continue
            candidates.append((plpc, p))

        candidates.sort(key=lambda x: x[0])

        for plpc, p in candidates:
            symbol = getattr(p, "symbol", "")
            qty = float(getattr(p, "qty", 0) or 0)

            if plpc > LOSER_EXIT_THRESHOLD:
                continue
            if symbol in _PENDING_SELLS:
                continue

            in_cooldown, cooldown_until = is_in_cooldown(ledger, symbol)
            if in_cooldown:
                continue

            # Skip if trailing stop or bracket already protecting this position
            existing_orders = await _get_open_orders_for_symbol(symbol)
            active_exits = [
                o for o in existing_orders
                if str(o.get("side", "")).lower() == "sell"
                and str(o.get("status", "")).lower() in ("new", "held", "accepted")
                and str(o.get("type", "")).lower() in ("trailing_stop", "stop", "limit")
            ]
            if active_exits:
                logger.debug(
                    "loser_sweep_skip symbol=%s reason=active_%s_exists",
                    symbol, active_exits[0].get("type"),
                )
                continue

            _PENDING_SELLS.add(symbol)
            try:
                logger.info(
                    "loser_sweep_sell symbol=%s qty=%.4f unrealized_plpc=%.6f",
                    symbol, qty, plpc,
                )
                order_id = await place_market_sell(symbol, qty)
                if order_id:
                    fill_price = None
                    try:
                        pos_objects = self._load_position_objects()
                        pos = next((p for p in pos_objects if getattr(p, "symbol", "") == symbol), None)
                        if pos:
                            fill_price = float(getattr(pos, "current_price", None) or 0) or None
                    except Exception:
                        pass
                    close_entry(
                        ledger,
                        symbol=symbol,
                        order_id=order_id,
                        exit_price=fill_price,
                        reason="loser_sweep",
                        cooldown_minutes=self.cooldown_minutes,
                    )
                    _BOUGHT_THIS_SESSION.add(symbol)
                    logger.info("loser_sweep_submitted symbol=%s order_id=%s qty=%.4f", symbol, order_id, qty)
                else:
                    logger.warning("loser_sweep_failed symbol=%s qty=%.4f reason=no_order_id", symbol, qty)
            except Exception:
                logger.exception("loser_sweep_exception symbol=%s qty=%.4f", symbol, qty)
            finally:
                _PENDING_SELLS.discard(symbol)

    async def submit_order(self, **kwargs):
        try:
            return await execution_place_bracket_buy(**kwargs)
        except Exception:
            logger.exception("submit_order failed kwargs=%s", kwargs)
            raise

    async def _run_agent(self, agent: Any, symbol: str) -> Optional[dict[str, Any]]:
        try:
            result = await agent.analyze(symbol)
            if isinstance(result, dict):
                return result
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent_failed symbol=%s agent=%s", symbol, getattr(agent, "name", "unknown"))
            return None

    async def _analyze_symbol(self, symbol: str, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
        async with sem:
            tasks = [asyncio.create_task(self._run_agent(agent, symbol)) for agent in self.agents]
            try:
                gathered = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                raise
            results: list[dict[str, Any]] = []
            for item in gathered:
                if isinstance(item, dict):
                    results.append(item)
            return results

    async def _scan_batch(self, batch: list[str]) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(self.max_concurrent_symbols)
        tasks = [asyncio.create_task(self._analyze_symbol(symbol, sem)) for symbol in batch]
        try:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise
        signals: list[dict[str, Any]] = []
        for item in gathered:
            if isinstance(item, list):
                signals.extend(item)
        return signals

    def _effective_signal_confidence(self, signal: dict[str, Any]) -> float:
        confidence = float(signal.get("confidence", 0.0) or 0.0)
        metadata = signal.get("metadata", {}) or {}
        agent = str(signal.get("agent", "")).lower()
        if agent == "news":
            news_conf = float(metadata.get("news_confidence", 0.0) or 0.0)
            news_label = str(metadata.get("news_label", "neutral") or "neutral").lower()
            confidence = max(confidence, news_conf)
            if news_label in {"positive", "negative"}:
                confidence = min(0.95, confidence * 1.10)
        if agent == "insider":
            confidence = max(0.0, min(1.0, confidence + self._insider_confidence_adjustment(signal)))
        return max(0.0, min(1.0, confidence))

    def _insider_confidence_adjustment(self, signal: dict[str, Any]) -> float:
        metadata = signal.get("metadata", {}) or {}
        stats = metadata.get("stats", {}) or {}
        thresholds = metadata.get("thresholds", {}) or {}
        trade_count = int(stats.get("trade_count", 0) or 0)
        win_rate = float(stats.get("win_rate", 0.0) or 0.0)
        min_trades = int(thresholds.get("min_trades", 100) or 100)
        min_win_rate = float(thresholds.get("min_win_rate", 0.70) or 0.70)
        insider_base_boost = _cfg_float("INSIDER_BASE_BOOST", 0.06)
        insider_low_penalty = _cfg_float("INSIDER_LOW_PENALTY", -0.08)
        if trade_count < min_trades or win_rate < min_win_rate:
            return insider_low_penalty
        trade_bonus = min(0.10, (trade_count - min_trades) / max(min_trades, 1) * 0.05)
        win_bonus = min(0.12, max(0.0, win_rate - min_win_rate) * 0.80)
        return insider_base_boost + trade_bonus + win_bonus

    def _current_position_count(self) -> int:
        return len(self._load_open_positions())

    def _parse_flatten_time(self) -> time:
        try:
            hh, mm = str(self.session_flatten_time).split(":")
            return time(int(hh), int(mm))
        except Exception:
            return time(15, 45)

    def _should_flatten_now(self) -> bool:
        now = self._et_now()
        if now.weekday() >= 5:
            return False
        current = now.time()
        flatten = self._parse_flatten_time()
        market_close = time(16, 0)
        return flatten <= current < market_close

    def _et_now(self) -> datetime:
        return datetime.now(ET)

    def _position_opened_today(self, position: Any) -> bool:
        opened_at = getattr(position, "opened_at", None) or getattr(position, "created_at", None) or getattr(position, "entry_time", None)
        if opened_at is None:
            return False
        try:
            if isinstance(opened_at, str):
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            else:
                opened_dt = opened_at
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=ET)
            return opened_dt.astimezone(ET).date() == self._et_now().date()
        except Exception:
            return False

    def _position_age_minutes(self, position: Any) -> float | None:
        opened_at = getattr(position, "opened_at", None) or getattr(position, "created_at", None) or getattr(position, "entry_time", None)
        if opened_at is None:
            return None
        try:
            if isinstance(opened_at, str):
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            else:
                opened_dt = opened_at
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=ET)
            return (self._et_now() - opened_dt.astimezone(ET)).total_seconds() / 60.0
        except Exception:
            return None

    async def _market_allows_longs(self) -> bool:
        if not self.market_filter_enabled:
            return True
        if not hasattr(self, "data_client") or self.data_client is None:
            return True
        try:
            spy_bars = self.data_client.get_stock_bars("SPY", "1Min", limit=30).df
            if spy_bars is None or spy_bars.empty:
                return True
            spy_change = (spy_bars["close"].iloc[-1] - spy_bars["close"].iloc[0]) / spy_bars["close"].iloc[0]
            if spy_change < self.market_filter_threshold:
                logger.info("Market filter: SPY down %.2f%% — skipping long entries", spy_change * 100)
                return False
            return True
        except Exception:
            logger.exception("market filter failed")
            return True

    def _vix_session_multiplier(self) -> float:
        return 1.0

    async def _close_position_market(self, symbol: str, qty: float, reason: str, ledger: Any) -> None:
        try:
            order_id = await place_market_sell(symbol, qty)
            if order_id:
                close_entry(ledger, symbol=symbol, order_id=order_id, exit_price=None, reason=reason, cooldown_minutes=self.cooldown_minutes)
                _BOUGHT_THIS_SESSION.add(symbol)
        except Exception:
            logger.exception("close_position_market failed symbol=%s reason=%s", symbol, reason)

    async def _session_exit_pass(self, ledger: Any) -> None:
        positions = self._load_position_objects()
        if not positions:
            return
        for p in positions:
            try:
                if not self._position_opened_today(p):
                    continue
                age = self._position_age_minutes(p)
                qty = float(getattr(p, "qty", 0) or 0)
                if qty <= 0:
                    continue
                if age is not None and age >= self.time_exit_minutes:
                    await self._close_position_market(getattr(p, "symbol", ""), qty, "time_exit_90m", ledger)
            except Exception:
                logger.exception("session_exit_pass failed symbol=%s", getattr(p, "symbol", "unknown"))

    async def _end_of_day_sweep(self, ledger: Any) -> None:
        if not self._should_flatten_now():
            return
        positions = self._load_position_objects()
        for p in positions:
            try:
                if not self._position_opened_today(p):
                    continue
                qty = float(getattr(p, "qty", 0) or 0)
                if qty > 0:
                    await self._close_position_market(getattr(p, "symbol", ""), qty, "eod_sweep", ledger)
            except Exception:
                logger.exception("end_of_day_sweep failed symbol=%s", getattr(p, "symbol", "unknown"))

    async def _protect_positions(self) -> None:
        """Scan all open positions and attach missing protection orders."""
        try:
            positions = await _get_open_positions()
            for p in positions:
                symbol = p.get("symbol")
                qty = float(p.get("qty", 0))
                if not symbol or qty == 0:
                    continue
                orders = await _get_open_orders_for_symbol(symbol)
                order_types = [str(o.get("type", "")).lower() for o in orders]
                order_sides = [str(o.get("side", "")).lower() for o in orders]

                if qty > 0:
                    has_trailing = any(
                        t == "trailing_stop"
                        for t, s in zip(order_types, order_sides)
                        if s == "sell"
                    )
                    has_hard_stop = any(
                        t == "stop"
                        for t, s in zip(order_types, order_sides)
                        if s == "sell"
                    )
                    if not has_trailing:
                        logger.warning("Long %s — attempting trailing stop sell (has_hard_stop=%s)", symbol, has_hard_stop)
                        trail_id = await place_trailing_stop_sell(symbol, qty, trail_percent=5.0)
                        if trail_id:
                            if has_hard_stop:
                                for o in orders:
                                    if str(o.get("type", "")).lower() == "stop" and str(o.get("side", "")).lower() == "sell":
                                        await _cancel_order_by_id(str(o.get("id", "")))
                                        logger.info("Long %s — hard stop replaced by trailing stop", symbol)
                        else:
                            if not has_hard_stop:
                                price = await get_latest_price(symbol)
                                if price:
                                    stop_price = round(price * 0.95, 2)
                                    from backend.execution import _post_order
                                    await _post_order({
                                        "symbol": symbol,
                                        "qty": str(int(qty)),
                                        "side": "sell",
                                        "type": "stop",
                                        "time_in_force": "gtc",
                                        "stop_price": str(stop_price),
                                    })
                                    logger.warning("Long %s — hard stop fallback placed at %.2f", symbol, stop_price)
                            else:
                                logger.warning("Long %s — trailing stop failed, hard stop still active", symbol)

                elif qty < 0:
                    has_trailing = any(
                        t == "trailing_stop"
                        for t, s in zip(order_types, order_sides)
                        if s == "buy"
                    )
                    has_hard_stop = any(
                        t == "stop"
                        for t, s in zip(order_types, order_sides)
                        if s == "buy"
                    )
                    if not has_trailing:
                        logger.warning("Short %s — attempting trailing stop buy (has_hard_stop=%s)", symbol, has_hard_stop)
                        trail_id = await place_trailing_stop_buy(symbol, abs(qty), 4.0)
                        if trail_id:
                            if has_hard_stop:
                                for o in orders:
                                    if str(o.get("type", "")).lower() == "stop" and str(o.get("side", "")).lower() == "buy":
                                        await _cancel_order_by_id(str(o.get("id", "")))
                                        logger.info("Short %s — hard stop replaced by trailing stop", symbol)
                        else:
                            if not has_hard_stop:
                                price = await get_latest_price(symbol)
                                if price:
                                    stop_price = round(price * 1.02, 2)
                                    from backend.execution import _post_order
                                    await _post_order({
                                        "symbol": symbol,
                                        "qty": str(int(abs(qty))),
                                        "side": "buy",
                                        "type": "stop",
                                        "time_in_force": "gtc",
                                        "stop_price": str(stop_price),
                                    })
                                    logger.warning("Short %s — hard stop fallback placed at %.2f", symbol, stop_price)
                            else:
                                logger.warning("Short %s — trailing stop failed, hard stop still active", symbol)
        except Exception:
            logger.exception("_protect_positions failed")

    def _count_trading_days(self, start: datetime, end: datetime) -> int:
        """Count trading days between two datetimes, excluding weekends."""
        count = 0
        current = start.date()
        end_date = end.date()
        while current < end_date:
            if current.weekday() < 5:  # Monday-Friday
                count += 1
            current += timedelta(days=1)
        return count

    async def _close_short_market(self, qty: float, symbol: str, ledger: Any) -> None:
        """Buy to cover a short position at market."""
        try:
            from backend.execution import place_market_buy
            order_id, _ = await place_market_buy(symbol, qty)
            if order_id:
                close_entry(ledger, symbol=symbol, order_id=order_id, exit_price=None, reason="time_exit_cover", cooldown_minutes=self.cooldown_minutes)
                _BOUGHT_THIS_SESSION.add(symbol)
                logger.info("Short time exit cover: %s qty=%.0f", symbol, qty)
        except Exception:
            logger.exception("_close_short_market failed symbol=%s", symbol)  

    async def _close_short_market(self, qty: float, symbol: str, ledger: Any) -> None:
        """Buy to cover a short position at market."""
        try:
            from backend.execution import place_market_buy
            order_id, _ = await place_market_buy(symbol, qty)
            if order_id:
                close_entry(ledger, symbol=symbol, order_id=order_id, exit_price=None, reason="time_exit_cover", cooldown_minutes=self.cooldown_minutes)
                _BOUGHT_THIS_SESSION.add(symbol)
                logger.info("Short time exit cover: %s qty=%.0f", symbol, qty)
        except Exception:
            logger.exception("_close_short_market failed symbol=%s", symbol)         

    async def _intraday_time_exit_pass(self, ledger: Any) -> None:
        """Exit positions after validated hold window unless above gain threshold."""
        try:
            positions = await _get_open_positions()
            pos_map = {p.get("symbol"): p for p in positions}
            now = datetime.now(ET)
            for symbol, entries in ledger.items():
                if not isinstance(entries, list):
                    continue
                open_entry = next(
                    (e for e in reversed(entries)
                     if e.get("status") == "open"
                     and str(e.get("strategy", "")).lower() in ("intraday", "wave", "ares")),
                    None,
                )
                if not open_entry:
                    continue
                created_at = _from_iso(open_entry.get("created_at"))
                if not created_at:
                    continue
                trading_days = self._count_trading_days(created_at, now)
                strategy = str(open_entry.get("strategy", "")).lower()
                hold_days = 21 if strategy == "ares" else 5
                if trading_days < hold_days:
                    continue
                pos = pos_map.get(symbol)
                if not pos:
                    continue
                qty = float(pos.get("qty", 0))
                if qty == 0:
                    continue
                if qty < 0:
                    logger.info("Time exit (short cover): %s held %d trading days (hold_days=%d)", symbol, trading_days, hold_days)
                    await self._close_short_market(abs(qty), symbol, ledger)
                    continue
                # Short position — buy to cover
                if qty < 0:
                    logger.info("Time exit (short cover): %s held %d trading days (hold_days=%d)", symbol, trading_days, hold_days)
                    await self._close_short_market(abs(qty), symbol, ledger)
                    continue
                entry_price = float(open_entry.get("entry_price", 0) or 0)
                current_price = float(pos.get("current_price", 0) or 0)
                if entry_price > 0 and current_price > 0:
                    gain_pct = (current_price - entry_price) / entry_price
                    threshold = 0.30 if strategy == "ares" else 0.15
                    if gain_pct > threshold:
                        logger.info(
                            "Time exit skipped for %s — up %.1f%% (>%.0f%% threshold), letting trailing stop run",
                            symbol, gain_pct * 100, threshold * 100,
                        )
                        continue
                logger.info("Time exit: %s held %d trading days (hold_days=%d)", symbol, trading_days, hold_days)
                await self._close_position_market(symbol, qty, "intraday_time_exit", ledger)
        except Exception:
            logger.exception("_intraday_time_exit_pass failed")        

    async def _handle_signals(self, signals: list[dict[str, Any]]) -> None:
        global HALT_ENTRIES
        ledger = load_ledger()
        open_positions = self._load_open_positions()

        # Check if we're within entry hours (9:30 AM - 3:30 PM ET)
        _now_et = datetime.now(ET)
        _market_open = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        _entry_cutoff = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        _allow_new_entries = _market_open <= _now_et <= _entry_cutoff

        # Fetch SPY trend once per cycle
        spy_trend_up = True
        try:
            spy_bars = await get_bars("SPY", timeframe="5Min", limit=12)
            if spy_bars and len(spy_bars) >= 2:
                spy_trend_up = float(spy_bars[-1].get("c", 0)) > float(spy_bars[0].get("c", 0))
                logger.debug(
                    "SPY trend: %s (%.2f -> %.2f)",
                    "UP" if spy_trend_up else "DOWN",
                    float(spy_bars[0].get("c", 0)),
                    float(spy_bars[-1].get("c", 0)),
                )
        except Exception:
            logger.debug("SPY trend fetch failed — defaulting to uptrend")

        logger.debug("handle_signals_start signals=%d open_positions=%d", len(signals), len(open_positions))
        await self._liquidate_loser_sweep(ledger)
        await self._intraday_time_exit_pass(ledger)
        await self._session_exit_pass(ledger)
        await self._end_of_day_sweep(ledger)

        if not under_position_limit(self.trading_client):
            logger.debug("handle_signals_stop reason=position_limit")
            save_ledger(ledger)
            return

        if HALT_ENTRIES:
            logger.info("Entries halted — daily loss limit reached")
            save_ledger(ledger)
            return

        if not await self._market_allows_longs():
            logger.debug("handle_signals_stop reason=market_filter")
            save_ledger(ledger)
            return

        self.order_size_multiplier = self._vix_session_multiplier()

        candidates: list[dict[str, Any]] = []
        short_candidates: list[dict[str, Any]] = []

        # Build per-symbol volume data lookup from volume agent signals
        symbol_volume_data: dict[str, dict] = {}
        for signal in signals:
            if str(signal.get("agent", "")).lower() == "volume":
                sym = signal.get("symbol", "")
                if sym:
                    meta = signal.get("metadata", {}) or {}
                    symbol_volume_data[sym] = {
                        "volume_ratio": float(meta.get("volume_ratio", 0.0) or 0.0),
                        "volume_acceleration": float(meta.get("volume_acceleration", 0.0) or 0.0),
                        "volume_slope": float(meta.get("volume_slope", 0.0) or 0.0),
                        "average_volume": float(meta.get("average_volume", 1.0) or 1.0),
                        "breakout": bool(meta.get("breakout", False)),
                    }

        for signal in signals:
            symbol = signal.get("symbol")
            direction = str(signal.get("direction", "hold"))
            score = float(signal.get("score", 0.5) or 0.5)
            confidence = self._effective_signal_confidence(signal)
            metadata = signal.get("metadata", {}) or {}

            if not symbol:
                continue
            if symbol in BLACKLIST:
                logger.info("Skipping %s — blacklisted", symbol)
                continue
            if already_have_position(self.trading_client, symbol):
                logger.info("Skipping %s — position already open", symbol)
                continue
            if symbol in _PENDING_BUYS:
                continue
            if symbol in _BOUGHT_THIS_SESSION:
                logger.info("Skipping %s — already bought this session", symbol)
                continue

            _vol_data = symbol_volume_data.get(symbol, {})
            volume_ratio = float(_vol_data.get("volume_ratio") or metadata.get("volume_ratio", 0.0) or 0.0)
            volume_acceleration = float(_vol_data.get("volume_acceleration") or metadata.get("volume_acceleration", 0.0) or 0.0)
            volume_slope = float(_vol_data.get("volume_slope") or metadata.get("volume_slope", 0.0) or 0.0)
            breakout = bool(_vol_data.get("breakout") or metadata.get("breakout", False))

            in_cooldown, _ = is_in_cooldown(ledger, symbol)
            if in_cooldown:
                continue

            open_qty = 0.0
            if symbol in open_positions:
                open_qty = float(open_positions.get(symbol, 0.0) or 0.0)

            if open_qty > 0:
                exit_reason = None
                _is_intraday = str(signal.get("agent", "")).lower() == "intraday"
                if not _is_intraday and volume_slope < 0:
                    exit_reason = "volume_slope_negative"
                elif not _is_intraday and volume_ratio < self.volume_ratio_exit:
                    exit_reason = "volume_ratio_fade"
                elif direction == "sell" and confidence >= 0.5:
                    exit_reason = "sell_signal"

                if exit_reason and symbol not in _PENDING_SELLS:
                    existing_orders = await _get_open_orders_for_symbol(symbol)
                    active_exits = [
                        o for o in existing_orders
                        if str(o.get("side", "")).lower() == "sell"
                        and str(o.get("status", "")).lower() in ("new", "held", "accepted")
                        and str(o.get("type", "")).lower() in ("trailing_stop", "stop", "limit")
                    ]
                    if active_exits:
                        logger.debug(
                            "Skipping market sell for %s — active %s order exists",
                            symbol, active_exits[0].get("type"),
                        )
                        continue

                    _PENDING_SELLS.add(symbol)
                    try:
                        order_id = await place_market_sell(symbol, open_qty)
                        if order_id:
                            close_entry(
                                ledger,
                                symbol=symbol,
                                order_id=order_id,
                                exit_price=None,
                                reason="volume_fade_exit" if exit_reason != "sell_signal" else "signal_exit",
                                cooldown_minutes=self.cooldown_minutes,
                            )
                    finally:
                        _PENDING_SELLS.discard(symbol)
                continue

            if direction == "buy":
                if score < max(0.62, self.early_entry_threshold):
                    continue
                if confidence < 0.25:
                    continue
                # Intraday agent signals bypass volume ratio gate — they have own volume logic
                _is_intraday = str(signal.get("agent", "")).lower() == "intraday"
                _intraday_active = bool(metadata.get("intraday_active", False))
                if not _is_intraday and not (volume_ratio >= self.volume_ratio_entry or breakout or volume_acceleration >= 0.95):
                    continue
                if not spy_trend_up and score < 0.72:
                    logger.debug("Skipping %s — SPY downtrend, score %.2f below 0.72 threshold", symbol, score)
                    continue
                candidates.append({
                    "signal": signal,
                    "symbol": symbol,
                    "score": score,
                    "confidence": confidence,
                    "priority": trade_priority(signal),
                })

            elif direction == "sell" and open_qty == 0:
                if score < 0.75:
                    continue
                if confidence < 0.65:
                    continue
                if symbol in _SHORT_BLACKLIST:
                    logger.debug("Skipping short %s — bond/low-vol ETF blacklist", symbol)
                    continue
                if symbol in _BOUGHT_THIS_SESSION:
                    logger.debug("Skipping short %s — already traded this session", symbol)
                    continue
                # Cap total short positions at 10
                current_shorts = sum(
                    1 for v in open_positions.values()
                    if float(v) < 0
                ) if isinstance(open_positions, dict) else 0
                if current_shorts >= 10:
                    logger.debug("Skipping short %s — max 10 shorts reached", symbol)
                    continue
                if not spy_trend_up:
                    pass
                else:
                    if score < 0.65:
                        logger.debug("Skipping short %s — SPY uptrend, score %.2f below 0.65", symbol, score)
                        continue
                if symbol in BLACKLIST:
                    continue
                if symbol in _PENDING_BUYS:
                    continue
                short_candidates.append({
                    "signal": signal,
                    "symbol": symbol,
                    "score": score,
                    "confidence": confidence,
                    "priority": trade_priority(signal),
                })

        # Process long candidates — only during entry hours
        for item in candidates:
            # Re-check time on every order — scan may have started before cutoff
            _now_check = datetime.now(ET)
            _allow_new_entries = _now_check.replace(hour=9, minute=30) <= _now_check <= _now_check.replace(hour=16, minute=0)
            if not _allow_new_entries:
                logger.info("Skipping remaining buys — passed 4:00 PM ET cutoff")
                break

            signal = item["signal"]
            symbol = item["symbol"]
            score = float(item["score"])
            confidence = float(item["confidence"])
            metadata = signal.get("metadata", {}) or {}

            price = await get_latest_price(symbol)
            if not price:
                continue
            if price < 4.0:
                logger.debug("Skipping %s — price %.2f below $4.00 minimum", symbol, price)
                continue

            buying_power = float(get_account_buying_power() or 0.0)
            if buying_power <= 0:
                continue

            max_position_usd = _cfg_any_float("MAX_POSITION_SIZE_USD", "MAX_POSITION_SIZE", default=10000.0)
            position_pct = _cfg_float("POSITION_SIZE_PCT", 0.10)
            buy_power_cap = float(getattr(self, "buy_power_cap", 1.0) or 1.0)
            allowed_dollars = min(
                max_position_usd,
                buying_power * buy_power_cap * position_pct,
                buying_power * buy_power_cap,
            )
            momentum_score = compute_momentum_score(signal)
            mult = min(size_multiplier(momentum_score), 2.0)
            allowed_dollars = min(allowed_dollars * mult, max_position_usd)
            qty = int(allowed_dollars // float(price))
            if qty < 1:
                continue

            exit_plan = build_exit_plan(signal)

            _PENDING_BUYS.add(symbol)
            _BOUGHT_THIS_SESSION.add(symbol)
            try:
                order_id, fill_price = await self.submit_order(
                    symbol=symbol,
                    qty=qty,
                    take_profit_pct=exit_plan["take_profit_pct"],
                    stop_loss_pct=exit_plan["stop_loss_pct"],
                    use_trailing=exit_plan["use_trailing"],
                    trailing_stop_pct=exit_plan["trailing_stop_pct"],
                )
            finally:
                _PENDING_BUYS.discard(symbol)

            if order_id:
                add_entry(
                    ledger,
                    symbol=symbol,
                    side="long",
                    qty=qty,
                    entry_price=fill_price or price,
                    order_id=order_id,
                    strategy=str(signal.get("agent", "unknown")),
                    cooldown_minutes=self.cooldown_minutes,
                    metadata={
                        "score": score,
                        "momentum_score": momentum_score,
                        "size_multiplier": mult,
                        "confidence": confidence,
                        "buying_power": buying_power,
                        "buy_power_cap": buy_power_cap,
                        "max_position_usd": max_position_usd,
                        "allowed_dollars": allowed_dollars,
                        "order_value": qty * float(price),
                        "exit_take_profit_pct": exit_plan["take_profit_pct"],
                        "exit_stop_loss_pct": exit_plan["stop_loss_pct"],
                        "use_trailing": exit_plan["use_trailing"],
                        "trailing_stop_pct": exit_plan["trailing_stop_pct"],
                        "volume_ratio": volume_ratio,
                        "volume_acceleration": volume_acceleration,
                        "volume_slope": volume_slope,
                        "agent": str(signal.get("agent", "unknown")),
                        "signal_direction": str(signal.get("direction", "")),
                        "news_confidence": float(metadata.get("news_confidence", 0.0) or 0.0),
                        "news_spike": bool(metadata.get("news_spike", False)),
                        "velocity_score": float(metadata.get("velocity_score", 0.0) or 0.0),
                        "velocity_ratio": float(metadata.get("velocity_ratio", 1.0) or 1.0),
                        "social_score": float(metadata.get("social_score", 0.5) or 0.5),
                        "insider_adjustment": float(metadata.get("insider_adjustment", 0.0) or 0.0),
                        "spy_trend_up": spy_trend_up,
                        "news_label": str(metadata.get("news_label", "neutral")),
                        "breakout": bool(metadata.get("breakout", False)),
                        **metadata,
                    },
                )
                logger.info("Long entry: %s qty=%d price=%.2f score=%.3f", symbol, qty, fill_price or price, score)

        # Process short candidates — only during entry hours
        for item in short_candidates:
            if not _allow_new_entries:
                logger.debug("Skipping all shorts — outside entry hours (after 3:30 PM ET)")
                break

            signal = item["signal"]
            symbol = item["symbol"]
            score = float(item["score"])
            confidence = float(item["confidence"])
            metadata = signal.get("metadata", {}) or {}

            price = await get_latest_price(symbol)
            if not price:
                continue
            if price < 10.0:
                logger.debug("Skipping short %s — price %.2f below $10 minimum", symbol, price)
                continue
            if already_have_position(self.trading_client, symbol):
                logger.debug("Skipping short %s — already have long position", symbol)
                continue
            if symbol in _PENDING_BUYS or symbol in _BOUGHT_THIS_SESSION:
                continue

            buying_power = float(get_account_buying_power() or 0.0)
            if buying_power <= 0:
                continue

            max_position_usd = _cfg_any_float("MAX_POSITION_SIZE_USD", "MAX_POSITION_SIZE", default=10000.0)
            position_pct = _cfg_float("POSITION_SIZE_PCT", 0.10)
            buy_power_cap = float(getattr(self, "buy_power_cap", 1.0) or 1.0)
            allowed_dollars = min(
                max_position_usd,
                buying_power * buy_power_cap * position_pct,
                buying_power * buy_power_cap,
            )
            momentum_score = compute_short_score(signal)
            mult = min(size_multiplier(momentum_score), 2.0)
            allowed_dollars = min(allowed_dollars * mult, max_position_usd)
            qty = int(allowed_dollars // float(price))
            if qty < 1:
                continue

            _PENDING_BUYS.add(symbol)
            _BOUGHT_THIS_SESSION.add(symbol)
            try:
                order_id, fill_price = await execution_place_bracket_short(
                    symbol=symbol,
                    qty=qty,
                    take_profit_pct=0.018,
                    stop_loss_pct=0.010,
                    use_trailing=True,
                    trailing_stop_pct=4.0,
                )
            finally:
                _PENDING_BUYS.discard(symbol)

            if order_id:
                add_entry(
                    ledger,
                    symbol=symbol,
                    side="short",
                    qty=qty,
                    entry_price=fill_price or price,
                    order_id=order_id,
                    strategy=str(signal.get("agent", "unknown")),
                    cooldown_minutes=self.cooldown_minutes,
                    metadata={
                        "score": score,
                        "momentum_score": momentum_score,
                        "confidence": confidence,
                        "short": True,
                        "spy_trend_up": spy_trend_up,
                        "signal_direction": "sell",
                        **metadata,
                    },
                )
                logger.info("Short entry: %s qty=%d price=%.2f score=%.3f", symbol, qty, fill_price or price, score)

        save_ledger(ledger)

    async def run_once(self, paper_only: bool = True) -> dict[str, Any]:
        self._reset_cycle_cache()
        self._open_positions_cache = None

        # Only clear bought-this-session once per trading day
        _now = datetime.now(ET)
        if self._session_date is None or self._session_date != _now.date():
            self._session_date = _now.date()
            _BOUGHT_THIS_SESSION.clear()
            logger.info("New trading day — cleared bought-this-session cache")

        all_signals: list[dict[str, Any]] = []
        errors = 0
        symbols = list(self.symbols)

        import random
        random.shuffle(symbols)
        symbols = symbols[:500]

        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i: i + self.batch_size]
            try:
                batch_signals = await self._scan_batch(batch)
                all_signals.extend(batch_signals)
            except asyncio.CancelledError:
                raise
            except Exception:
                errors += 1
                logger.exception("batch_failed start=%s size=%s", i, len(batch))
            # Protect positions every 20 batches
            if i > 0 and (i // self.batch_size) % 20 == 0:
                await self._protect_positions()

        # Only process signals if within entry hours
        _now_et = datetime.now(ET)
        _market_open = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        _entry_cutoff = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        _within_hours = _market_open <= _now_et <= _entry_cutoff

        if all_signals and _within_hours:
            try:
                await self._handle_signals(all_signals)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("handle_signals_failed")
        elif all_signals and not _within_hours:
            logger.info("Skipping signal processing — outside market hours (%s ET)", _now_et.strftime("%H:%M"))

        # Final protection check after each full scan cycle
        await self._protect_positions()

        return {
            "paper_only": paper_only,
            "signals": all_signals,
            "count": len(all_signals),
            "errors": errors,
            "symbols": symbols,
            "batch_size": self.batch_size,
            "max_concurrent_symbols": self.max_concurrent_symbols,
            "buy_power_cap": self.buy_power_cap,
            "early_entry_threshold": self.early_entry_threshold,
            "volume_ratio_entry": self.volume_ratio_entry,
            "volume_ratio_exit": self.volume_ratio_exit,
        }