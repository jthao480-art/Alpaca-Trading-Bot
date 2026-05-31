"""
event_bus.py – Async publish/subscribe event bus.
Backend components publish events; the WebSocket bridge and any other
consumers subscribe to receive them.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine, Dict, List

logger = logging.getLogger(__name__)


class EventBus:
    """Lightweight in-process async pub/sub bus."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._wildcard: List[Callable] = []

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------
    def subscribe(self, topic: str, handler: Callable) -> None:
        """Subscribe *handler* to a specific *topic*."""
        self._subscribers[topic].append(handler)
        logger.debug("Subscribed %s to topic '%s'", handler, topic)

    def subscribe_all(self, handler: Callable) -> None:
        """Subscribe *handler* to every published event."""
        self._wildcard.append(handler)

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        try:
            self._subscribers[topic].remove(handler)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    async def publish(self, topic: str, payload: Any) -> None:
        """Publish *payload* to *topic*.  Runs all handlers as tasks."""
        handlers = list(self._subscribers.get(topic, [])) + list(self._wildcard)
        if not handlers:
            return
        for handler in handlers:
            try:
                result = handler(topic, payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("EventBus handler error on topic '%s'", topic)

    


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
bus: EventBus = EventBus()

# ---------------------------------------------------------------------------
# Topic constants
# ---------------------------------------------------------------------------
TOPIC_AGENT_SIGNAL = "agent.signal"
TOPIC_COORDINATOR_DECISION = "coordinator.decision"
TOPIC_STATE_SNAPSHOT = "state.snapshot"
TOPIC_TRADE_OPENED = "trade.opened"
TOPIC_TRADE_CLOSED = "trade.closed"
TOPIC_LEARNING_SUMMARY = "learning.summary"
TOPIC_RISK_VETO = "risk.veto"
TOPIC_ERROR = "system.error"
TOPIC_HEARTBEAT = "system.heartbeat"
