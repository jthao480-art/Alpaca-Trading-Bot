from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend import config
from backend.agents.news_agent import NewsAgent
from backend.agents.momentum_agent import MomentumAgent
from backend.agents.volume_agent import VolumeAgent
from backend.agents.forecast_agent import ForecastAgent
from backend.agents.fundamentals_agent import FundamentalsAgent
from backend.agents.wallet_agent import WalletAgent
from backend.agents.insideragent import InsiderAgent
from backend.execution import (
    get_latest_price,
    _get_open_orders_for_symbol,
    place_bracket_buy as execution_place_bracket_buy,
    place_market_sell,
)
from backend.services.account_service import get_account_buying_power
from backend.services.trade_ledger_service import (
    add_entry,
    close_entry,
    is_in_cooldown,
    load_ledger,
    save_ledger,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _cfg_float(name: str, default: float) -> float:
    return float(getattr(config, name, default) or default)


def _cfg_int(name: str, default: int) -> int:
    return int(getattr(config, name, default) or default)


def compute_momentum_score(signal: dict[str, Any]) -> float:
    metadata = signal.get("metadata", {}) or {}
    base_score = float(signal.get("score", 0.0) or 0.0)
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    volume_ratio = float(metadata.get("volume_ratio", 1.0) or 1.0)
    volume_acceleration = float(metadata.get("volume_acceleration", 1.0) or 1.0)
    volume_slope = float(metadata.get("volume_slope", 0.0) or 0.0)
    news_strength = float(metadata.get("news_strength", 0.0) or 0.0)
    breakout = 1.0 if bool(metadata.get("breakout", False)) else 0.0

    raw = (
        0.30 * base_score
        + 0.25 * confidence
        + 0.18 * max(0.0, volume_ratio - 1.0)
        + 0.10 * max(0.0, volume_acceleration - 1.0)
        + 0.07 * max(0.0, volume_slope)
        + 0.05 * news_strength
        + 0.05 * breakout
    )
    return max(0.0, min(raw, 2.0))


def size_multiplier(momentum_score: float) -> float:
    if momentum_score < 0.25:
        return 0.4
    if momentum_score < 0.45:
        return 0.7
    if momentum_score < 0.75:
        return 1.0
    if momentum_score < 1.10:
        return 1.5
    if momentum_score < 1.45:
        return 2.0
    return 2.5


def build_exit_plan(signal: dict[str, Any], bars_held: int = 0, green_gain_pct: float = 0.0) -> dict[str, float | bool]:
    momentum_score = compute_momentum_score(signal)

    if momentum_score < 0.25:
        tp, sl = 0.010, 0.006
    elif momentum_score < 0.45:
        tp, sl = 0.014, 0.007
    elif momentum_score < 0.75:
        tp, sl = 0.020, 0.009
    elif momentum_score < 1.10:
        tp, sl = 0.028, 0.012
    elif momentum_score < 1.45:
        tp, sl = 0.040, 0.015
    else:
        tp, sl = 0.055, 0.018

    tighten_after_bars = _cfg_int("EXIT_TIGHTEN_AFTER_BARS", 2)
    tight_loss_stop_pct = _cfg_float("TIGHT_LOSS_STOP_PCT", 0.004)
    trailing_trigger_pct = _cfg_float("TRAILING_TRIGGER_PCT", 0.004)
    trailing_stop_default = _cfg_float("TRAILING_STOP_PCT", 0.004 if momentum_score < 1.0 else 0.006)

    if bars_held >= tighten_after_bars and green_gain_pct <= 0.0:
        sl = min(sl, tight_loss_stop_pct)

    use_trailing = green_gain_pct >= trailing_trigger_pct

    return {
        "take_profit_pct": tp,
        "stop_loss_pct": sl,
        "use_trailing": use_trailing,
        "trailing_stop_pct": trailing_stop_default if use_trailing else 0.0,
    }


class botA:
    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        trading_client: Any | None = None,
        use_news: bool = True,
        use_volume: bool = True,
        use_momentum: bool = True,
        use_forecast: bool = True,
        use_fundamentals: bool = True,
        use_wallet: bool = True,
        use_insider: bool = True,
        max_concurrent_symbols: int = 15,
        batch_size: int = 25,
        cooldown_minutes: int = 10,
        buy_power_cap: float = 0.35,
        early_entry_threshold: float = 0.42,
        volume_ratio_entry: float = 1.08,
        volume_ratio_exit: float = 0.90,
    ) -> None:
        self.trading_client = trading_client
        self.symbols = list(symbols) if symbols is not None else list(config.SYMBOL_UNIVERSE)
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
        self.exit_locks: dict[str, bool] = {}

        self.market_filter_threshold = _cfg_float("MARKET_FILTER_THRESHOLD", -0.010)
        self.market_filter_enabled = bool(getattr(config, "MARKET_FILTER_ENABLED", True))
        self.daily_position_cap = _cfg_int("DAILY_POSITION_CAP", 250)
        self.time_exit_minutes = _cfg_int("TIME_EXIT_MINUTES", 60)
        self.session_flatten_time = getattr(config, "SESSION_FLATTEN_TIME", "15:50")
        self.vix_threshold = _cfg_float("VIX_THRESHOLD", 30.0)
        self.vix_size_multiplier = _cfg_float("VIX_SIZE_MULTIPLIER", 0.7)
        self.order_size_multiplier = 1.0
        self.blacklist = set(map(str.upper, getattr(config, "BLACKLIST", {"POEL", "BEEP", "SAFX", "CBUS", "ALGS", "AMOD", "RXT", "TGEN", "SHMD", "ARBE", "RDCM", "OUST", "FDMT", "CTM", "CYPH"})))

        self._open_positions_cache: dict[str, float] | None = None
        self._bars_cache: dict[str, Any] = {}
        self._news_cache: dict[str, Any] = {}

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
            for p in self.trading_client.get_all_positions():
                try:
                    positions[p.symbol] = float(p.qty or 0)
                except Exception:
                    continue
        except Exception:
            logger.exception("failed to load open positions")
        self._open_positions_cache = positions
        return positions

    def _has_position(self, symbol: str) -> bool:
        return symbol.upper() in self._load_open_positions()

    def _exit_locked(self, symbol: str) -> bool:
        return self.exit_locks.get(symbol.upper(), False)

    def _lock_exit(self, symbol: str) -> None:
        self.exit_locks[symbol.upper()] = True

    def _unlock_exit(self, symbol: str) -> None:
        self.exit_locks.pop(symbol.upper(), None)

    async def submit_order(self, **kwargs):
        return await execution_place_bracket_buy(**kwargs)

    async def _run_agent(self, agent: Any, symbol: str) -> Optional[dict[str, Any]]:
        try:
            result = await agent.analyze(symbol)
            return result if isinstance(result, dict) else None
        except Exception:
            logger.exception("agent_failed symbol=%s agent=%s", symbol, getattr(agent, "name", "unknown"))
            return None

    async def _analyze_symbol(self, symbol: str, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
        async with sem:
            tasks = [asyncio.create_task(self._run_agent(agent, symbol)) for agent in self.agents]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            return [item for item in gathered if isinstance(item, dict)]

    async def _scan_batch(self, batch: list[str]) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(self.max_concurrent_symbols)
        tasks = [asyncio.create_task(self._analyze_symbol(symbol, sem)) for symbol in batch]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        signals: list[dict[str, Any]] = []
        for item in gathered:
            if isinstance(item, list):
                signals.extend(item)
        await asyncio.sleep(1.0)
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
                confidence = min(1.0, confidence * 1.12)

        if agent == "insider":
            confidence = max(0.0, min(1.0, confidence + self._insider_confidence_adjustment(signal)))

        return max(0.0, min(1.0, confidence))

    def _insider_confidence_adjustment(self, signal: dict[str, Any]) -> float:
        metadata = signal.get("metadata", {}) or {}
        stats = metadata.get("stats", {}) or {}
        thresholds = metadata.get("thresholds", {}) or {}

        trade_count = int(stats.get("trade_count", 0) or 0)
        win_rate = float(stats.get("win_rate", 0.0) or 0.0)

        min_trades = int(thresholds.get("min_trades", 75) or 75)
        min_win_rate = float(thresholds.get("min_win_rate", 0.65) or 0.65)

        insider_base_boost = _cfg_float("INSIDER_BASE_BOOST", 0.08)
        insider_low_penalty = _cfg_float("INSIDER_LOW_PENALTY", -0.10)

        if trade_count < min_trades or win_rate < min_win_rate:
            return insider_low_penalty

        trade_bonus = min(0.12, (trade_count - min_trades) / max(min_trades, 1) * 0.06)
        win_bonus = min(0.15, max(0.0, win_rate - min_win_rate) * 0.90)
        return insider_base_boost + trade_bonus + win_bonus

    async def _has_open_exit_orders(self, symbol: str) -> bool:
        try:
            orders = await _get_open_orders_for_symbol(symbol)
            for order in orders:
                if str(order.get("symbol", "")).upper() != symbol.upper():
                    continue
                if str(order.get("side", "")).lower() != "sell":
                    continue
                if str(order.get("status", "")).lower() in {"filled", "canceled", "rejected", "expired"}:
                    continue
                return True
        except Exception:
            logger.exception("failed checking open exits for %s", symbol)
        return False

    async def _close_position_market(self, symbol: str, qty: float, reason: str, ledger: Any) -> None:
        try:
            order_id = await place_market_sell(symbol, qty)
            if order_id and ledger is not None:
                close_entry(
                    ledger,
                    symbol=symbol,
                    order_id=order_id,
                    exit_price=None,
                    reason=reason,
                    cooldown_minutes=self.cooldown_minutes,
                )
        except Exception:
            logger.exception("close_position_market failed symbol=%s reason=%s", symbol, reason)

    async def _handle_signals(self, signals: list[dict[str, Any]]) -> None:
        ledger = load_ledger()
        open_positions = self._load_open_positions()
        await self._close_losing_positions(-0.04) if hasattr(self, "_close_losing_positions") else None

        for signal in signals:
            symbol = str(signal.get("symbol", "")).upper()
            direction = str(signal.get("direction", "hold"))
            score = float(signal.get("score", 0.5) or 0.5)
            confidence = self._effective_signal_confidence(signal)
            metadata = signal.get("metadata", {}) or {}

            if not symbol or symbol in self.blacklist:
                continue
            if is_in_cooldown(ledger, symbol)[0]:
                continue
            if self._exit_locked(symbol):
                continue

            open_qty = float(open_positions.get(symbol, 0.0) or 0.0)
            volume_ratio = float(metadata.get("volume_ratio", 0.0) or 0.0)
            volume_acceleration = float(metadata.get("volume_acceleration", 0.0) or 0.0)
            volume_slope = float(metadata.get("volume_slope", 0.0) or 0.0)
            breakout = bool(metadata.get("breakout", False))

            if open_qty > 0:
                if await self._has_open_exit_orders(symbol):
                    continue
                if direction == "sell" or volume_slope < 0 or volume_ratio < self.volume_ratio_exit:
                    self._lock_exit(symbol)
                    try:
                        order_id = await place_market_sell(symbol, open_qty)
                        if order_id:
                            close_entry(
                                ledger,
                                symbol=symbol,
                                order_id=order_id,
                                exit_price=None,
                                reason="signal_exit",
                                cooldown_minutes=self.cooldown_minutes,
                            )
                    finally:
                        self._unlock_exit(symbol)
                continue

            if direction != "buy":
                continue

            entry_ok = (
                score >= self.early_entry_threshold
                and confidence >= 0.45
                and (volume_ratio >= self.volume_ratio_entry or breakout or volume_acceleration >= 1.01)
                and volume_slope >= -0.01
            )

            if not entry_ok:
                continue
            if self._has_position(symbol):
                continue

            price = await get_latest_price(symbol)
            if not price:
                continue

            buying_power = float(get_account_buying_power() or 0.0)
            if buying_power <= 0:
                continue

            max_position_usd = float(getattr(config, "MAX_POSITION_SIZE_USD", getattr(config, "MAX_POSITION_SIZE", 0.0)) or 0.0)
            position_pct = float(getattr(config, "POSITION_SIZE_PCT", 0.0) or 0.0)
            base_allowed = min(buying_power * self.buy_power_cap, buying_power * position_pct if position_pct > 0 else buying_power)
            if max_position_usd > 0:
                base_allowed = min(base_allowed, max_position_usd)

            momentum_score = compute_momentum_score(signal)
            mult = size_multiplier(momentum_score)
            allowed_dollars = base_allowed * mult * self.order_size_multiplier

            if allowed_dollars <= 0:
                continue

            qty = int(allowed_dollars // float(price))
            if qty < 1:
                continue

            exit_plan = build_exit_plan(signal)
            order_id, fill_price = await self.submit_order(
                symbol=symbol,
                qty=qty,
                take_profit_pct=exit_plan["take_profit_pct"],
                stop_loss_pct=exit_plan["stop_loss_pct"],
                use_trailing=exit_plan["use_trailing"],
                trailing_stop_pct=exit_plan["trailing_stop_pct"],
            )

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
                        "allowed_dollars": allowed_dollars,
                        "use_trailing": exit_plan["use_trailing"],
                        "trailing_stop_pct": exit_plan["trailing_stop_pct"],
                        "volume_ratio": volume_ratio,
                        "volume_acceleration": volume_acceleration,
                        "volume_slope": volume_slope,
                        **metadata,
                    },
                )

        save_ledger(ledger)

    async def run_once(self, paper_only: bool = True) -> dict[str, Any]:
        self._reset_cycle_cache()
        all_signals: list[dict[str, Any]] = []
        errors = 0
        for i in range(0, len(self.symbols), self.batch_size):
            batch = self.symbols[i:i + self.batch_size]
            try:
                batch_signals = await self._scan_batch(batch)
                all_signals.extend(batch_signals)
                await self._handle_signals(batch_signals)
            except Exception:
                errors += 1
                logger.exception("batch_failed start=%s size=%s", i, len(batch))
        return {"paper_only": paper_only, "signals": all_signals, "count": len(all_signals), "errors": errors, "symbols": self.symbols}