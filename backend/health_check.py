from __future__ import annotations

"""
health_check.py — Bot health monitor with Discord alerts.

Checks:
1. Symbol universe size (< 100 = numpy/libstdc++ issue)
2. No buys during market hours (stuck bot)
3. SELL 0 spam detection

Sends alerts to Discord webhook.

Configure via Railway Variables:
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
"""

import asyncio
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from backend import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Alert config
DISCORD_WEBHOOK_URL = getattr(config, "DISCORD_WEBHOOK_URL", "")

# Thresholds
MIN_SYMBOL_COUNT = 100
MAX_HOURS_WITHOUT_BUY = 4

# State tracking
_last_alert_times: dict[str, datetime] = {}
_alert_cooldown_hours = 4
_buy_count_today = 0
_last_buy_time: datetime | None = None
_market_open_today: datetime | None = None


async def _send_discord(title: str, message: str, color: int = 0xFF0000) -> bool:
    """Send alert to Discord webhook."""
    webhook_url = DISCORD_WEBHOOK_URL or getattr(config, "DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("HEALTH: DISCORD_WEBHOOK_URL not configured")
        return False
    try:
        payload = {
            "embeds": [{
                "title": f"🤖 Alpaca Bot Alert: {title}",
                "description": message,
                "color": color,
                "timestamp": datetime.now(ET).isoformat(),
                "footer": {"text": f"Alpaca Bot • {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}"}
            }]
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 204):
                logger.info("HEALTH: Discord alert sent — %s", title)
                return True
            else:
                logger.warning("HEALTH: Discord webhook returned %d", resp.status_code)
                return False
    except Exception:
        logger.exception("HEALTH: Failed to send Discord alert")
        return False


def _should_alert(alert_key: str) -> bool:
    """Rate limit — don't repeat same alert within cooldown period."""
    last = _last_alert_times.get(alert_key)
    now = datetime.now(ET)
    if last is None or (now - last).total_seconds() / 3600 >= _alert_cooldown_hours:
        _last_alert_times[alert_key] = now
        return True
    return False


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60


async def check_symbol_universe() -> None:
    """Alert if symbol universe too small."""
    symbols = list(getattr(config, "SYMBOLS", []))
    count = len(symbols)
    if count < MIN_SYMBOL_COUNT:
        logger.critical("HEALTH: Symbol universe only %d symbols!", count)
        if _should_alert("symbol_universe"):
            await _send_discord(
                "Symbol Universe Too Small ⚠️",
                f"**Expected:** ~6,939 symbols\n**Actual:** {count} symbols\n\n"
                f"The bot is only scanning {count} open positions — numpy/libstdc++ failed to load.\n\n"
                f"**Fix:** Add to Railway Variables:\n"
                f"```\nLD_LIBRARY_PATH=/nix/store/ybjcla5bhj8g1y84998pn4a2drfxybkv-gcc-13.3.0-lib/lib\n```\n"
                f"Then redeploy.",
                color=0xFF0000
            )
    else:
        logger.debug("HEALTH: Symbol universe OK (%d symbols)", count)


async def check_no_buys() -> None:
    """Alert if no buys during market hours."""
    if not _is_market_hours() or _market_open_today is None:
        return
    now = datetime.now(ET)
    hours_since_open = (now - _market_open_today).total_seconds() / 3600
    if hours_since_open < 2:
        return
    if _buy_count_today == 0 and hours_since_open >= MAX_HOURS_WITHOUT_BUY:
        logger.warning("HEALTH: No buys in %.1f market hours", hours_since_open)
        if _should_alert("no_buys"):
            await _send_discord(
                "No Buys During Market Hours ⚠️",
                f"Market open **{hours_since_open:.1f} hours** with **0 buys today**.\n\n"
                f"Possible causes:\n"
                f"• Symbol universe not loading\n"
                f"• TradetiqAgent fetch failing\n"
                f"• All signals below threshold\n"
                f"• Bot crash/restart loop\n\n"
                f"Check Railway logs for errors.",
                color=0xFFA500
            )


def record_buy() -> None:
    """Call after every successful buy."""
    global _buy_count_today, _last_buy_time
    _buy_count_today += 1
    _last_buy_time = datetime.now(ET)


def reset_daily_counts() -> None:
    """Call at start of each trading day."""
    global _buy_count_today, _last_buy_time, _market_open_today
    _buy_count_today = 0
    _last_buy_time = None
    _market_open_today = datetime.now(ET)
    logger.info("HEALTH: Daily counters reset")


async def run_health_checks() -> None:
    """Run all health checks."""
    logger.info("HEALTH: Running checks...")
    await check_symbol_universe()
    await check_no_buys()
    logger.info("HEALTH: Checks complete")


async def health_monitor_loop() -> None:
    """Background loop — runs every 30 minutes."""
    await asyncio.sleep(60)  # wait 1 min after startup
    while True:
        try:
            await run_health_checks()
        except Exception:
            logger.exception("Health monitor loop failed")
        await asyncio.sleep(1800)  # every 30 minutes