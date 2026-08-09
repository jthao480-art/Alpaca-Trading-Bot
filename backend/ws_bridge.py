from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Set

import websockets

from . import config
from . import event_bus as eb

logger = logging.getLogger(__name__)

_clients: Set[Any] = set()
_loop: asyncio.AbstractEventLoop | None = None


def _on_event(topic: str, payload: Any) -> None:
    loop = _loop
    if loop is not None:
        loop.call_soon_threadsafe(asyncio.create_task, _broadcast(payload))


async def _broadcast(payload: Any) -> None:
    msg = json.dumps(payload, default=str)
    dead = set()

    for ws in list(_clients):
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)

    _clients.difference_update(dead)


async def _handler(websocket: Any) -> None:
    _clients.add(websocket)
    logger.info("WS client connected total=%d", len(_clients))
    try:
        await websocket.send(json.dumps({"ts": "connected"}, default=str))
        async for _ in websocket:
            pass
    except Exception:
        pass
    finally:
        _clients.discard(websocket)
        logger.info("WS client disconnected total=%d", len(_clients))


async def start_ws_server() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    eb.bus.subscribe_all(_on_event)

    async with websockets.serve(_handler, config.WS_HOST, config.WS_PORT):
        logger.info("WebSocket bridge listening on ws://%s:%d", config.WS_HOST, config.WS_PORT)
        await asyncio.Future()
