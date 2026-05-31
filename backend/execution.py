"""
execution.py – Alpaca order placement with bracket orders and retries.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from .services.auth import alpaca_request_async

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0


async def _post_order(payload: dict) -> Optional[dict]:
    """Submit an order to Alpaca with retries."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await alpaca_request_async(
                "POST",
                "/v2/orders",
                data=payload,
                use_data_api=False,
            )
            if resp.status_code in (200, 201):
                return resp.json()

            logger.warning(
                "Order rejected (attempt %d/%d): %s %s",
                attempt,
                _MAX_RETRIES,
                resp.status_code,
                resp.text,
            )
        except Exception:
            logger.exception("Order attempt %d/%d failed", attempt, _MAX_RETRIES)

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_DELAY * attempt)

    logger.error("All order attempts failed for payload: %s", payload)
    return None


async def place_bracket_buy(
    symbol: str,
    qty: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Place a bracket buy order.
    Returns (order_id, fill_price) or (None, None) on failure.
    """
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round(take_profit_price, 2))},
        "stop_loss": {"stop_price": str(round(stop_loss_price, 2))},
    }

    logger.info(
        "Placing bracket BUY: %s qty=%s tp=%.2f sl=%.2f",
        symbol,
        qty,
        take_profit_price,
        stop_loss_price,
    )

    result = await _post_order(payload)
    if result:
        order_id = result.get("id")
        fill_price = float(result.get("filled_avg_price") or 0) or None
        logger.info("Order placed: %s id=%s", symbol, order_id)
        return order_id, fill_price

    return None, None


async def place_market_sell(symbol: str, qty: float) -> Optional[str]:
    """Close a position with a market sell."""
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }

    logger.info("Placing market SELL: %s qty=%s", symbol, qty)

    result = await _post_order(payload)
    if result:
        return result.get("id")

    return None


async def get_latest_price(symbol: str) -> Optional[float]:
    """Fetch last trade price for position sizing."""
    try:
        resp = await alpaca_request_async(
            "GET",
            f"/v2/stocks/{symbol}/trades/latest",
            use_data_api=True,
        )
        resp.raise_for_status()
        trade = resp.json().get("trade", {})
        return float(trade.get("p", 0)) or None
    except Exception:
        logger.exception("execution: failed to get latest price for %s", symbol)
        return None


def calculate_qty(price: float, position_size_usd: float) -> float:
    """Calculate share quantity; minimum 1 share."""
    if price <= 0:
        return 1.0
    qty = position_size_usd / price
    return max(1.0, round(qty, 0))