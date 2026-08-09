from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentSignal(BaseModel):
    agent: str
    symbol: str
    score: float
    direction: str
    confidence: float = 0.5
    reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class CoordinatorDecision(BaseModel):
    symbol: str
    action: str
    weighted_score: float
    signals: List[AgentSignal] = Field(default_factory=list)
    veto: bool = False
    veto_reason: str = ""
    timestamp: datetime = Field(default_factory=utc_now)


class TradeRecord(BaseModel):
    id: Optional[int] = None
    symbol: str
    action: str
    qty: float
    entry_price: float
    exit_price: Optional[float] = None
    take_profit: float
    stop_loss: float
    status: str = "open"
    pnl: Optional[float] = None
    alpaca_order_id: Optional[str] = None
    entry_ts: datetime = Field(default_factory=utc_now)
    exit_ts: Optional[datetime] = None
    features: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StateSnapshot(BaseModel):
    open_positions: Dict[str, TradeRecord] = Field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_loss_hit: bool = False
    last_scan_ts: Optional[datetime] = None
    model_version: str = "unknown"
    timestamp: datetime = Field(default_factory=utc_now)


class BusEvent(BaseModel):
    topic: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=utc_now)


class LearningSummary(BaseModel):
    model_version: str
    trained_at: datetime = Field(default_factory=utc_now)
    n_samples: int = 0
    accuracy: Optional[float] = None
    notes: str = ""
