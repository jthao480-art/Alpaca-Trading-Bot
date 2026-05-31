"""
schemas.py – Shared Pydantic models used across agents, orchestrator, and API.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent signal
# ---------------------------------------------------------------------------
class AgentSignal(BaseModel):
    agent: str
    symbol: str
    score: float                          # 0.0 – 1.0
    direction: str                        # "buy" | "sell" | "hold"
    confidence: float = 0.5
    reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Coordinator decision
# ---------------------------------------------------------------------------
class CoordinatorDecision(BaseModel):
    symbol: str
    action: str                           # "buy" | "sell" | "hold"
    weighted_score: float
    signals: List[AgentSignal] = Field(default_factory=list)
    veto: bool = False
    veto_reason: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------
class TradeRecord(BaseModel):
    id: Optional[int] = None
    symbol: str
    action: str
    qty: float
    entry_price: float
    exit_price: Optional[float] = None
    take_profit: float
    stop_loss: float
    status: str = "open"                  # "open" | "closed" | "cancelled"
    pnl: Optional[float] = None
    alpaca_order_id: Optional[str] = None
    entry_ts: datetime = Field(default_factory=datetime.utcnow)
    exit_ts: Optional[datetime] = None
    features: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------
class StateSnapshot(BaseModel):
    open_positions: Dict[str, TradeRecord] = Field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_loss_hit: bool = False
    last_scan_ts: Optional[datetime] = None
    model_version: str = "unknown"
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Event envelope published on the bus
# ---------------------------------------------------------------------------
class BusEvent(BaseModel):
    topic: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Learning summary
# ---------------------------------------------------------------------------
class LearningSummary(BaseModel):
    model_version: str
    trained_at: datetime = Field(default_factory=datetime.utcnow)
    n_samples: int = 0
    accuracy: Optional[float] = None
    notes: str = ""
