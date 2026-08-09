from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence, Any

from .agents.news_agent import NewsAgent
from .agents.volume_agent import VolumeAgent
from .agents.momentum_agent import MomentumAgent
from .agents.fundamentals_agent import FundamentalsAgent
from .execution import place_bracket_buy, place_market_sell

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    symbols: list[str]
    signals: list[dict]
    buys: list[dict]
    sells: list[dict]
    holds: list[dict]


class Orchestrator:
    def __init__(
        self,
        symbols: Optional[Sequence[str]] = None,
        use_news: bool = False,
        use_volume: bool = True,
        use_momentum: bool = True,
        use_fundamentals: bool = True,
        max_positions: Optional[int] = None,
        position_size_usd: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
    ):
        self.symbols = list(symbols or [])
        self.use_news = use_news
        self.use_volume = use_volume
        self.use_momentum = use_momentum
        self.use_fundamentals = use_fundamentals
        self.max_positions = max_positions
        self.position_size_usd = position_size_usd
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct

        self.agents: list[Any] = []
        if self.use_news:
            self.agents.append(NewsAgent())
        if self.use_volume:
            self.agents.append(VolumeAgent())
        if self.use_momentum:
            self.agents.append(MomentumAgent())
        if self.use_fundamentals:
            self.agents.append(FundamentalsAgent())

    def _default_symbol_universe(self) -> list[str]:
        return self.symbols or ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA"]

    async def close_position_market(self, symbol: str, qty: float, reason: str | None = None, metadata: dict | None = None):
        try:
            return await place_market_sell(symbol, qty)
        except Exception:
            logger.exception("Failed to close position for %s reason=%s", symbol, reason)
            return None

    async def place_entry(self, symbol: str, qty: float):
        tp = self.take_profit_pct if self.take_profit_pct is not None else 0.04
        sl = self.stop_loss_pct if self.stop_loss_pct is not None else 0.06
        try:
            return await place_bracket_buy(symbol, qty, tp, sl, use_trailing=True, trailing_stop_pct=4.0)
        except Exception:
            logger.exception("Failed to place entry for %s", symbol)
            return None, None

    async def run_universe(self, paper_only: bool = False) -> dict:
        symbols = self._default_symbol_universe()
        signals: list[dict] = []
        buys: list[dict] = []
        sells: list[dict] = []
        holds: list[dict] = []

        for symbol in symbols:
            for agent in self.agents:
                try:
                    signal = await agent.analyze(symbol)
                    if not signal:
                        continue

                    payload = signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
                    payload["agent"] = getattr(agent, "name", agent.__class__.__name__.lower())
                    signals.append(payload)

                    direction = payload.get("direction", "hold")
                    if direction == "buy":
                        buys.append(payload)
                    elif direction == "sell":
                        sells.append(payload)
                    else:
                        holds.append(payload)
                except Exception:
                    logger.exception("Orchestrator agent failure for %s", symbol)

        if not paper_only:
            for sig in buys:
                try:
                    await self.place_entry(sig.get("symbol", ""), 1)
                except Exception:
                    logger.exception("Failed executing buy for %s", sig.get("symbol", ""))

            for sig in sells:
                try:
                    await self.close_position_market(sig.get("symbol", ""), 1, "signal_sell", sig)
                except Exception:
                    logger.exception("Failed executing sell for %s", sig.get("symbol", ""))

        return {
            "symbols": symbols,
            "signals": signals,
            "buys": buys,
            "sells": sells,
            "holds": holds,
            "paper_only": paper_only,
            "config": {
                "max_positions": self.max_positions,
                "position_size_usd": self.position_size_usd,
                "take_profit_pct": self.take_profit_pct,
                "stop_loss_pct": self.stop_loss_pct,
            },
        }