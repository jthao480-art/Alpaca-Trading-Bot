from __future__ import annotations

import logging
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from backend import config
from backend.services.trade_ledger_service import load_ledger, save_ledger, close_entry
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

async def reconcile_ledger_with_broker() -> int:
    """
    Sync the trade ledger with Alpaca's actual filled orders.
    Closes any ledger entries where Alpaca has already executed the exit
    (trailing stop, bracket TP/SL, or market sell fired by Alpaca directly).
    Returns the number of entries reconciled.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        return 0

    try:
        client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )
    except Exception as exc:
        logger.warning("Ledger reconciliation skipped: %s", exc)
        return 0

    ledger = load_ledger()
    reconciled = 0

    # Get current open positions from Alpaca
    try:
        open_positions = client.get_all_positions()
        open_symbols = {getattr(p, "symbol", "").upper() for p in open_positions}
    except Exception as exc:
        logger.warning("Failed to fetch positions for reconciliation: %s", exc)
        return 0

    # Find ledger entries that are "open" but position no longer exists on Alpaca
    for symbol, entries in ledger.items():
        for entry in entries:
            if entry.get("status") != "open":
                continue

            if symbol.upper() in open_symbols:
                continue  # still open on Alpaca — leave it

            # Position closed on Alpaca but ledger still shows open
            # Find the closing order to get exit price
            exit_price = None
            close_reason = "alpaca_exit"
            try:
                since = datetime.now(timezone.utc) - timedelta(days=7)
                orders = client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    symbols=[symbol],
                    after=since,
                    limit=10,
                ))
                for order in orders:
                    side = str(getattr(order, "side", "")).lower()
                    status = str(getattr(order, "status", "")).lower()
                    order_type = str(getattr(order, "type", "")).lower()
                    if side == "sell" and status == "filled":
                        exit_price = float(getattr(order, "filled_avg_price", 0) or 0) or None
                        if "trailing" in order_type:
                            close_reason = "trailing_stop"
                        elif "stop" in order_type:
                            close_reason = "stop_loss"
                        elif "limit" in order_type:
                            close_reason = "take_profit"
                        else:
                            close_reason = "market_exit"
                        break
            except Exception:
                pass

            close_entry(
                ledger,
                symbol=symbol,
                order_id=None,
                exit_price=exit_price,
                reason=close_reason,
            )
            logger.info(
                "Ledger reconciled: %s closed via %s @ %s",
                symbol, close_reason, exit_price,
            )
            reconciled += 1

    if reconciled > 0:
        save_ledger(ledger)
        logger.info("Ledger reconciliation complete — %d entries updated", reconciled)

    return reconciled

async def sync_ledger_with_broker() -> list[str]:
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        logger.warning("Missing Alpaca credentials; skipping broker sync")
        return []

    try:
        client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )
    except Exception as exc:
        logger.warning("Failed to initialize Alpaca TradingClient; skipping broker sync: %s", exc)
        return []

    try:
        positions = client.get_all_positions()
    except APIError as exc:
        logger.warning("Alpaca broker sync skipped due to API error: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Alpaca broker sync skipped due to unexpected error: %s", exc)
        return []

    symbols: list[str] = []
    for position in positions:
        symbol = getattr(position, "symbol", None)
        if isinstance(symbol, str) and symbol.strip():
            symbols.append(symbol.strip().upper())

    return sorted(set(symbols))