"""
orchestrator.py – Coordinator / orchestrator.
Runs agent scans, computes weighted scores, executes trades, manages state.
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from . import config
from . import event_bus as eb
from .agents.news_agent import NewsAgent
from .agents.wallet_agent import WalletAgent
from .agents.momentum_agent import MomentumAgent
from .agents.volume_agent import VolumeAgent
from .agents.forecast_agent import ForecastAgent
from .agents.fundamentals_agent import FundamentalsAgent
from .agents.risk_agent import RiskAgent
from .db.trades_repo import insert_trade, update_trade_exit
from .execution import (
    place_bracket_buy,
    place_market_sell,
    get_latest_price,
    calculate_qty,
)
from .schemas import AgentSignal, CoordinatorDecision, TradeRecord
from .state_store import StateStore

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Agent registry
# -----------------------------------------------------------------------
_AGENTS = {
    "news":         NewsAgent(),
    "wallet":       WalletAgent(),
    "momentum":     MomentumAgent(),
    "volume":       VolumeAgent(),
    "forecast":     ForecastAgent(),
    "fundamentals": FundamentalsAgent(),
}
_RISK_AGENT = RiskAgent()


class Orchestrator:
    def __init__(self, state: StateStore) -> None:
        self._state = state
        self._state.set_daily_limit(config.DAILY_LOSS_LIMIT_USD)

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------
    async def scan(self, symbols: List[str]) -> None:
        """Run one full scan cycle across all symbols."""
        tasks = [self._scan_symbol(sym) for sym in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._state.set_last_scan()
        await self._publish_snapshot()

    async def _scan_symbol(self, symbol: str) -> None:
        """Collect signals, compute score, decide, execute."""
        try:
            signals = await self._collect_signals(symbol)
            if not signals:
                return

            decision = self._compute_decision(symbol, signals)

            # Publish individual signals
            for sig in signals:
                await eb.bus.publish(
                    eb.TOPIC_AGENT_SIGNAL,
                    sig.model_dump(mode="json"),
                )

            # Publish coordinator decision
            await eb.bus.publish(
                eb.TOPIC_COORDINATOR_DECISION,
                decision.model_dump(mode="json"),
            )

            await self._maybe_execute(decision)

        except Exception:
            logger.exception("Orchestrator: unhandled error scanning %s", symbol)
            await eb.bus.publish(eb.TOPIC_ERROR, {"symbol": symbol, "error": "scan_failed"})

    # ------------------------------------------------------------------
    # Agent collection
    # ------------------------------------------------------------------
    async def _collect_signals(self, symbol: str) -> List[AgentSignal]:
        tasks = {name: agent.analyze(symbol) for name, agent in _AGENTS.items()}
        results = {}
        for name, coro in tasks.items():
            try:
                results[name] = await coro
            except Exception:
                logger.exception("Agent %s threw exception for %s", name, symbol)
                results[name] = None
        return [sig for sig in results.values() if sig is not None]

    # ------------------------------------------------------------------
    # Weighted score
    # ------------------------------------------------------------------
    def _compute_decision(
        self, symbol: str, signals: List[AgentSignal]
    ) -> CoordinatorDecision:
        weights = config.AGENT_WEIGHTS
        total_weight = 0.0
        weighted_sum = 0.0

        for sig in signals:
            w = weights.get(sig.agent, 0.0)
            weighted_sum += sig.score * w
            total_weight += w

        weighted_score = weighted_sum / total_weight if total_weight > 0 else 0.5

        if weighted_score >= config.BUY_THRESHOLD:
            action = "buy"
        elif weighted_score <= config.SELL_THRESHOLD:
            action = "sell"
        else:
            action = "hold"

        return CoordinatorDecision(
            symbol=symbol,
            action=action,
            weighted_score=weighted_score,
            signals=signals,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _maybe_execute(self, decision: CoordinatorDecision) -> None:
        symbol = decision.symbol
        action = decision.action
        snap = self._state.snapshot()

        if action == "hold":
            return

        if snap.daily_loss_hit:
            logger.warning("Daily loss limit active; skipping %s", symbol)
            return

        # ---- BUY ----
        if action == "buy":
            price = await get_latest_price(symbol)
            if not price:
                logger.warning("Cannot get price for %s; skipping buy", symbol)
                return

            has_pos = self._state.has_position(symbol)
            vetoed, reason = await _RISK_AGENT.check(
                symbol=symbol,
                action="buy",
                proposed_price=price,
                daily_pnl=snap.daily_pnl,
                has_position=has_pos,
            )
            if vetoed:
                decision.veto = True
                decision.veto_reason = reason
                await eb.bus.publish(eb.TOPIC_RISK_VETO, {"symbol": symbol, "reason": reason})
                logger.warning("VETO buy %s: %s", symbol, reason)
                return

            qty = calculate_qty(price, config.MAX_POSITION_SIZE_USD)
            tp = round(price * (1 + config.TAKE_PROFIT_PCT), 2)
            sl = round(price * (1 - config.STOP_LOSS_PCT), 2)

            order_id, fill_price = await place_bracket_buy(symbol, qty, tp, sl)
            if not order_id:
                return

            trade = TradeRecord(
                symbol=symbol,
                action="buy",
                qty=qty,
                entry_price=fill_price or price,
                take_profit=tp,
                stop_loss=sl,
                alpaca_order_id=order_id,
                features=_signals_to_features(decision.signals),
                metadata={"decision_score": decision.weighted_score},
            )
            db_id = await insert_trade(trade)
            trade.id = db_id
            self._state.add_position(trade)
            await self._state.save() if hasattr(self._state, "save") else None
            await eb.bus.publish(eb.TOPIC_TRADE_OPENED, trade.model_dump(mode="json"))
            logger.info("Opened position: %s qty=%s @ %.2f", symbol, qty, fill_price or price)

        # ---- SELL / EXIT ----
        elif action == "sell":
            existing = self._state.get_position(symbol)
            if not existing:
                return  # nothing to close

            vetoed, reason = await _RISK_AGENT.check(
                symbol=symbol,
                action="sell",
                proposed_price=existing.entry_price,
                daily_pnl=snap.daily_pnl,
                has_position=True,
            )
            if vetoed:
                decision.veto = True
                decision.veto_reason = reason
                await eb.bus.publish(eb.TOPIC_RISK_VETO, {"symbol": symbol, "reason": reason})
                return

            order_id = await place_market_sell(symbol, existing.qty)
            if not order_id:
                return

            exit_price = await get_latest_price(symbol) or existing.entry_price
            pnl = (exit_price - existing.entry_price) * existing.qty

            if existing.id:
                await update_trade_exit(existing.id, exit_price, pnl)

            self._state.add_pnl(pnl)
            self._state.remove_position(symbol)
            await self._state.save() if hasattr(self._state, "save") else None

            closed = existing.model_copy(
                update={"exit_price": exit_price, "pnl": pnl, "status": "closed",
                        "exit_ts": datetime.utcnow()}
            )
            await eb.bus.publish(eb.TOPIC_TRADE_CLOSED, closed.model_dump(mode="json"))
            logger.info("Closed position: %s pnl=%.2f", symbol, pnl)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    async def _publish_snapshot(self) -> None:
        snap = self._state.snapshot()
        await eb.bus.publish(eb.TOPIC_STATE_SNAPSHOT, snap.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _signals_to_features(signals: List[AgentSignal]) -> dict:
    return {f"{s.agent}_score": s.score for s in signals}



