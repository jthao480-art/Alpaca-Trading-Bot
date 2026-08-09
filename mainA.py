from __future__ import annotations



import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient

from backend import config
from backend.botA import botA
from backend.services.startup_sync import sync_ledger_with_broker

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


async def _close_positions_if_daily_loss(bot: botA, trading_client: TradingClient) -> bool:
    try:
        acct = trading_client.get_account()
        equity = float(getattr(acct, "equity", 0) or 0)
        last_equity = float(getattr(acct, "last_equity", 0) or 0)
        daily_pl = equity - last_equity
        loss_limit = float(getattr(config, "DAILY_LOSS_LIMIT", -1000.0))

        if daily_pl <= loss_limit:
            logger.warning("Daily loss limit hit: daily_pl=%s limit=%s", daily_pl, loss_limit)

            try:
                trading_client.cancel_all_orders()
            except Exception:
                logger.exception("Failed to cancel all open orders before daily-loss liquidation")

            try:
                await asyncio.sleep(1.0)
            except Exception:
                pass

            try:
                positions = list(trading_client.get_all_positions())
            except Exception:
                positions = []

            positions = sorted(
                positions,
                key=lambda p: float(getattr(p, "unrealized_plpc", 0) or 0),
            )

            for p in positions:
                try:
                    qty = abs(float(getattr(p, "qty", 0) or 0))
                    if qty > 0:
                        await bot._close_position_market(
                            getattr(p, "symbol", ""),
                            qty,
                            "daily_loss_halt",
                            None,
                        )
                except Exception:
                    logger.exception("Failed closing position during daily loss halt")

            return True

    except Exception:
        logger.exception("Daily loss monitor failed")

    return False


async def main() -> None:
    symbols = await sync_ledger_with_broker()
    symbols = list(symbols) if symbols else list(config.SYMBOL_UNIVERSE)

    trading_client = TradingClient(
        config.ALPACA_API_KEY,
        config.ALPACA_API_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )

    bot = botA(
        symbols=symbols,
        trading_client=trading_client,
        use_news=True,
        use_volume=True,
        use_momentum=True,
        use_forecast=True,
        use_fundamentals=True,
        use_wallet=True,
        use_insider=True,
        max_concurrent_symbols=75,
        batch_size=75,
        cooldown_minutes=10,
        buy_power_cap=0.35,
        early_entry_threshold=0.42,
        volume_ratio_entry=1.08,
        volume_ratio_exit=0.90,
    )

    while True:
        now = _now_et()
        session = _session_name(now)

        if session == "closed":
            await _sleep_until(_next_session_start(now))
            continue

        if await _close_positions_if_daily_loss(bot, trading_client):
            await asyncio.sleep(30)
            continue

        result = await bot.run_once(paper_only=config.PAPER_TRADING)
        logger.info(
            "Session=%s scanned=%s signals=%s errors=%s",
            session,
            len(result.get("symbols", [])),
            result.get("count", 0),
            result.get("errors", 0),
        )

        await asyncio.sleep(_sleep_seconds_for_session(session))


if __name__ == "__main__":
    asyncio.run(main())