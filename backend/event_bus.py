from __future__ import annotations

from collections.abc import Callable
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[str, Any], None]] = []

    def subscribe_all(self, callback: Callable[[str, Any], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, topic: str, payload: Any) -> None:
        for callback in list(self._subscribers):
            try:
                callback(topic, payload)
            except Exception:
                continue


bus = EventBus()
