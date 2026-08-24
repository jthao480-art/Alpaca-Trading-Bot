from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient

from backend import config
from backend.botv3 import botV3
from backend.orchestrator import Orchestrator
from backend.services.startup_sync import sync_ledger_with_broker, reconcile_ledger_with_broker
from backend.execution import (
    monitor_hard_stops,
    monitor_position_protection,
    attach_deferred_trailing_stops,
    _is_regular_market_hours,
)

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(ET)


def _minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _session_name(now: datetime) -> str:
    weekday = now.weekday()
    mins = _minutes(now)

    if weekday == 5:
        return "closed"
    if weekday == 6 and mins >= 20 * 60:
        return "overnight"
    if weekday in (0, 1, 2, 3, 4) and mins < 4 * 60:
        return "overnight"
    if 4 * 60 <= mins < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= mins < 16 * 60:
        return "regular"
    if 16 * 60 <= mins < 20 * 60:
        return "after_hours"
    if weekday in (0, 1, 2, 3) and mins >= 20 * 60:
        return "overnight"
    if weekday == 4 and mins >= 20 * 60:
        return "closed"
    return "closed"


def _next_session_start(now: datetime) -> datetime:
    weekday = now.weekday()
    mins = _minutes(now)

    def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
        d = now + timedelta(days=days)
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET)

    if weekday == 5:
        return at(20, 0, days=1)
    if weekday == 6 and mins < 20 * 60:
        return at(20, 0)
    if weekday == 4 and mins >= 20 * 60:
        return at(20, 0, days=2)
    return at(4, 0)


def _sleep_seconds_for_session(session: str) -> int:
    if session == "regular":
        return 15
    if session in ("premarket", "after_hours"):
        return 30
    if session == "overnight":
        return 90
    return 300


async def _sleep_until(target: datetime) -> None:
    delay = max(0.0, (target - _now_et()).total_seconds())
    if delay > 0:
        logger.info("Sleeping until %s ET", target.strftime("%Y-%m-%d %H:%M:%S"))
        await asyncio.sleep(delay)


def build_trading_client() -> TradingClient:
    api_key = getattr(config, "ALPACA_API_KEY", None) or os.getenv("APCA_API_KEY_ID")
    api_secret = getattr(config, "ALPACA_SECRET_KEY", None) or os.getenv("APCA_API_SECRET_KEY")
    paper = bool(getattr(config, "PAPER_TRADING", True))

    if not api_key or not api_secret:
        raise RuntimeError("Missing Alpaca API credentials")

    return TradingClient(api_key, api_secret, paper=paper)


async def _close_positions_if_daily_loss(bot: botV3, trading_client: TradingClient) -> bool:
    try:
        acct = trading_client.get_account()
        equity = float(getattr(acct, "equity", 0) or 0)
        last_equity = float(getattr(acct, "last_equity", 0) or 0)
        daily_pl = equity - last_equity
        loss_limit = float(getattr(config, "DAILY_LOSS_LIMIT_USD", -1000.0))
        # Only enforce daily loss limit during market hours
        # Pre-market: last_equity reflects yesterday's close, not today's start
        _now_et = datetime.now(ET)
        _market_open = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        _within_market = _now_et >= _market_open
        if _within_market and daily_pl <= -abs(loss_limit):
            logger.warning("Daily loss limit hit: daily_pl=%s limit=%s", daily_pl, loss_limit)

            try:
                trading_client.cancel_orders()
            except Exception:
                logger.exception("Failed to cancel all open orders before daily-loss liquidation")

            await asyncio.sleep(1.0)

            try:
                positions = list(trading_client.get_all_positions())
            except Exception:
                positions = []

            for p in positions:
                try:
                    qty = abs(float(getattr(p, "qty", 0) or 0))
                    if qty > 0:
                        await bot._close_position_market(getattr(p, "symbol", ""), qty, "daily_loss_halt", None)
                except Exception:
                    logger.exception("Failed closing position during daily loss halt")

            return True

    except Exception:
        logger.exception("Daily loss monitor failed")

    return False


def session_overrides(session: str) -> dict[str, float | bool]:
    if session == "overnight":
        return {
            "buy_power_cap": float(getattr(config, "BUY_POWER_CAP_OVERNIGHT", 0.35)),
            "early_entry_threshold": float(getattr(config, "EARLY_ENTRY_THRESHOLD_OVERNIGHT", 0.45)),
            "volume_ratio_entry": float(getattr(config, "VOLUME_RATIO_ENTRY_OVERNIGHT", 1.02)),
            "volume_ratio_exit": float(getattr(config, "VOLUME_RATIO_EXIT_OVERNIGHT", 0.92)),
            "use_news": True,
            "use_volume": True,
            "use_momentum": True,
            "use_forecast": True,
            "use_fundamentals": True,
            "use_wallet": True,
            "use_insider": True,
        }
    if session in ("premarket", "after_hours"):
        return {
            "buy_power_cap": float(getattr(config, "BUY_POWER_CAP_EXTENDED", 0.25)),
            "early_entry_threshold": float(getattr(config, "EARLY_ENTRY_THRESHOLD_EXTENDED", 0.48)),
            "volume_ratio_entry": float(getattr(config, "VOLUME_RATIO_ENTRY_EXTENDED", 1.05)),
            "volume_ratio_exit": float(getattr(config, "VOLUME_RATIO_EXIT_EXTENDED", 0.94)),
            "use_news": True,
            "use_volume": True,
            "use_momentum": True,
            "use_forecast": True,
            "use_fundamentals": True,
            "use_wallet": True,
            "use_insider": True,
        }
    return {
        "buy_power_cap": float(getattr(config, "BUY_POWER_CAP", 0.20)),
        "early_entry_threshold": float(getattr(config, "EARLY_ENTRY_THRESHOLD", 0.62)),
        "volume_ratio_entry": float(getattr(config, "VOLUME_RATIO_ENTRY", 1.05)),
        "volume_ratio_exit": float(getattr(config, "VOLUME_RATIO_EXIT", 0.95)),
        "use_news": True,
        "use_volume": True,
        "use_momentum": True,
        "use_forecast": True,
        "use_fundamentals": True,
        "use_wallet": True,
        "use_insider": True,
    }

async def _run_startup_protection_sweep() -> None:
    """Run a single protection sweep 30 seconds after startup to catch unprotected positions."""
    await asyncio.sleep(30)  # wait for bot to fully initialize
    from backend.execution import monitor_position_protection
    # Trigger one immediate sweep by calling the core logic directly
    from backend.execution import (
        _get_open_positions, _get_open_orders_for_symbol,
        place_trailing_stop_sell, TRAIL_PCT
    )
    try:
        positions = await _get_open_positions()
        unprotected = []
        for p in positions:
            symbol = p.get("symbol")
            qty = float(p.get("qty", 0))
            if not symbol or qty <= 0 or symbol == "CXE":
                continue
            orders = await _get_open_orders_for_symbol(symbol)
            active_sells = [
                o for o in orders
                if str(o.get("side", "")).lower() == "sell"
                and str(o.get("status", "")).lower()
                not in {"filled", "canceled", "rejected", "expired"}
            ]
            if not active_sells:
                unprotected.append((symbol, qty))

        if unprotected:
            logger.warning("Startup sweep — %d unprotected: %s", len(unprotected), [s for s, _ in unprotected])
            for symbol, qty in unprotected:
                trail_id = await place_trailing_stop_sell(symbol, qty, TRAIL_PCT)
                logger.info("Startup sweep — %s trail_id=%s", symbol, trail_id)
                await asyncio.sleep(2.0)
        else:
            logger.info("Startup sweep — all positions protected ✓")
    except Exception:
        logger.exception("Startup protection sweep failed")

async def main() -> None:
    trading_client = build_trading_client()

    # Start the hard-stop safety monitor as a background task —
    # runs continuously alongside the main bot loop, checks every 30 s
    asyncio.create_task(monitor_hard_stops())
    from backend.health_check import health_monitor_loop
    asyncio.create_task(health_monitor_loop())
    asyncio.create_task(monitor_position_protection(interval_seconds=7200))

    # Run one immediate protection sweep at startup
    if _is_regular_market_hours():
        logger.info("Startup — running immediate position protection sweep")
        asyncio.create_task(_run_startup_protection_sweep())

    # At startup, if we're in regular market hours, attach trailing stops
    # to any positions that were bought after-hours in a prior session
    if _is_regular_market_hours():
        logger.info("Market is open — running deferred trailing stop attachment")
        await attach_deferred_trailing_stops()

    try:
        await sync_ledger_with_broker()  # sync ledger only
        await reconcile_ledger_with_broker()  # close ledger entries for Alpaca-executed exits
    except Exception:
        logger.exception("sync_ledger_with_broker failed")

    symbols = list(getattr(config, "SYMBOLS", ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA"]))
    logger.info("Symbol universe loaded: %d symbols", len(symbols))
    _last_deferred_check: str = ""   # track which regular session we last ran the deferred check

    while True:
        now = _now_et()
        session = _session_name(now)

        if session == "closed":
            await _sleep_until(_next_session_start(now))
            continue

        # Run deferred trailing stop attachment once per regular session open
        if session == "regular":
            session_key = now.strftime("%Y-%m-%d")
            if session_key != _last_deferred_check:
                logger.info("Regular session start — attaching any deferred trailing stops")
                await attach_deferred_trailing_stops()
                _last_deferred_check = session_key

        # Reconcile ledger with Alpaca every cycle — catches trailing stop / bracket exits
        try:
            reconciled = await reconcile_ledger_with_broker()
            if reconciled > 0:
                logger.info("Reconciled %d ledger entries with Alpaca", reconciled)
        except Exception:
            logger.exception("reconcile_ledger_with_broker failed")

        temp_bot = botV3(symbols=symbols, trading_client=trading_client)
        if await _close_positions_if_daily_loss(temp_bot, trading_client):
            await asyncio.sleep(30)
            continue

        overrides = session_overrides(session)

        bot = botV3(
            symbols=symbols,
            trading_client=trading_client,
            use_news=bool(overrides["use_news"]),
            use_volume=bool(overrides["use_volume"]),
            use_momentum=bool(overrides["use_momentum"]),
            use_forecast=bool(overrides["use_forecast"]),
            use_fundamentals=bool(overrides["use_fundamentals"]),
            use_wallet=bool(overrides["use_wallet"]),
            use_insider=bool(overrides["use_insider"]),
            max_concurrent_symbols=int(getattr(config, "MAX_CONCURRENT_SYMBOLS", 50)),
            batch_size=int(getattr(config, "BATCH_SIZE", 50)),
            cooldown_minutes=int(getattr(config, "COOLDOWN_MINUTES", 20)),
            buy_power_cap=float(overrides["buy_power_cap"]),
            early_entry_threshold=float(overrides["early_entry_threshold"]),
            volume_ratio_entry=float(overrides["volume_ratio_entry"]),
            volume_ratio_exit=float(overrides["volume_ratio_exit"]),
        )

        result = await bot.run_once(paper_only=bool(getattr(config, "PAPER_TRADING", True)))
        logger.info(
            "Session=%s scanned=%s signals=%s buys=%s sells=%s holds=%s",
            session,
            len(result.get("symbols", [])),
            len(result.get("signals", [])),
            len(result.get("buys", [])),
            len(result.get("sells", [])),
            len(result.get("holds", [])),
        )

        await asyncio.sleep(_sleep_seconds_for_session(session))


if __name__ == "__main__":
    asyncio.run(main())
