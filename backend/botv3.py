from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from uvicorn.config import Config
from uvicorn.server import Server

from . import config
from backend import event_bus as eb
from backend.api import app as fastapi_app, set_state_store
from backend.db.init_db import init_db
from backend.learning import run_learning_job
from backend.orchestrator import Orchestrator
from backend.services.model_service import model_service
from backend.state_store import StateStore
from backend.ws_bridge import start_ws_server


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("botv3")

PROJECT_ROOT = Path(__file__).resolve().parent


def _now_et() -> datetime:
    return datetime.now(ET)


def _is_market_hours() -> bool:
    now = _now_et()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return dt_time(9, 30) <= current < dt_time(16, 0)


def _next_monday_preopen() -> datetime:
    now = _now_et()
    days_ahead = (7 - now.weekday()) % 7
    target_date = now.date() + timedelta(days=days_ahead)

    target = datetime.combine(target_date, dt_time(9, 0), tzinfo=ET)
    if target <= now:
        target += timedelta(days=7)

    return target


async def _heartbeat() -> None:
    while True:
        await eb.bus.publish(
            eb.TOPIC_HEARTBEAT,
            {"ts": datetime.now(UTC).isoformat()},
        )
        await asyncio.sleep(30)


async def _learning_scheduler() -> None:
    last_run_date: date | None = None
    while True:
        now = _now_et()
        if now.weekday() == 0 and now.time() < dt_time(9, 10):
            today = now.date()
            if last_run_date != today:
                logger.info("Monday learning job triggered.")
                await run_learning_job()
                last_run_date = today
        await asyncio.sleep(60)

print("Refreshing scanner universe...")

async def _scan_loop(orchestrator: Orchestrator) -> None:
    first = True
    while True:
        try:
            if first or _is_market_hours():
                first = False
                print("scan loop: forced scan", flush=True)
                logger.info("Scanning symbols: %s", config.SYMBOLS)
                await orchestrator.scan(config.SYMBOLS)
                print("scan loop: scan complete", flush=True)
            else:
                print("scan loop: outside market hours", flush=True)
                logger.debug("Outside market hours - sleeping.")
        except Exception:
            logger.exception("Scan loop error")
        await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)


async def main() -> None:
    logger.info("=== TradingBot v3 starting ===")

    await init_db(config.DATABASE_URL)

    state = StateStore(config.STATE_PATH)
    await state.load()
    set_state_store(state)

    model_service.load()
    state.set_model_version(model_service.version)

    orch = Orchestrator(state)

    uvicorn_config = Config(
        fastapi_app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="warning",
        lifespan="on",
    )
    server = Server(uvicorn_config)

    tasks = [
        asyncio.create_task(server.serve(), name="uvicorn"),
        asyncio.create_task(start_ws_server(), name="ws_bridge"),
        asyncio.create_task(_scan_loop(orch), name="scan_loop"),
        asyncio.create_task(_learning_scheduler(), name="learning_scheduler"),
        asyncio.create_task(_heartbeat(), name="heartbeat"),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")