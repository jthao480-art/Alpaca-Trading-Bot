from __future__ import annotations

from typing import Any

from .trade_manager import TradeManager
from .agents.portfolio_agent import PortfolioAgent
from .agents.risk_agent import RiskAgent
from .agents.fundamentals_agent import FundamentalsAgent
from .agents.momentum_agent import MomentumAgent
from .agents.news_agent import NewsAgent
from .agents.volume_agent import VolumeAgent


class OrchestratorV2:
    def __init__(self) -> None:
        self.trade_manager = TradeManager()
        self.agents = [
            PortfolioAgent(),
            RiskAgent(),
            FundamentalsAgent(),
            MomentumAgent(),
            NewsAgent(),
            VolumeAgent(),
        ]

    async def run_once(self, symbols: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for symbol in symbols:
            for agent in self.agents:
                try:
                    signal = await agent.analyze(symbol)
                    if isinstance(signal, dict):
                        results.append(signal)
                except Exception:
                    continue

        return {
            "symbols": symbols,
            "signals": results,
            "count": len(results),
        }


def list_positions() -> list[Any]:
    tm = TradeManager()
    positions = tm.get_open_positions()
    return positions if isinstance(positions, list) else list(positions or [])
