from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentSignal:
    symbol: str
    score: float
    direction: str
    confidence: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent:
    name: str = "base"

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def make_signal(
        self,
        symbol: str,
        score: float,
        direction: str,
        confidence: float,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "score": float(score),
            "direction": direction,
            "confidence": float(confidence),
            "reason": reason,
            "metadata": metadata or {},
            "agent": self.name,
        }

    def signal_to_dict(self, signal: dict[str, Any]) -> dict[str, Any]:
        return signal

    def _signal_to_dict(self, signal: dict[str, Any]) -> dict[str, Any]:
        return self.signal_to_dict(signal)
