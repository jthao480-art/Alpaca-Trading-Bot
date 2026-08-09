from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import TrailingStopOrderRequest

from backend import config
from backend.services.bars_service import get_latest_bar


logger = logging.getLogger(__name__)

API_KEY = getattr(config, "ALPACA_API_KEY", None)
API_SECRET = getattr(config, "ALPACA_SECRET_KEY", None) or getattr(config, "ALPACA_SECRET_KEY", None)
ALPACA_BASE_URL = getattr(config, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0
_FILL_POLL_ATTEMPTS = 30
_FILL_POLL_DELAY = 1.0
_CANCEL_SETTLE_ATTEMPTS = 20
_CANCEL_SETTLE_DELAY = 1.0

MAX_LEVERAGE = float(getattr(config, "MAX_LEVERAGE", 1.0))
MAX_SHORT_LEVERAGE = float(getattr(config, "MAX_SHORT_LEVERAGE", 0.5))
MAX_POSITION_SIZE = float(getattr(config, "MAX_POSITION_SIZE", getattr(config, "MAX_POSITION_SIZE_USD", 2000.0)))
MAX_POSITIONS = int(getattr(config, "MAX_POSITIONS", 55))
DAILY_LOSS_LIMIT = float(getattr(config, "DAILY_LOSS_LIMIT", -1000.0))
BUYING_POWER_BUFFER = float(getattr(config, "BUYING_POWER_BUFFER", 1.1))
POSITION_SIZE_PCT = float(getattr(config, "POSITION_SIZE_PCT", 0.05))
HARD_STOP_PCT = float(getattr(config, "HARD_STOP_PCT", 0.94))
TRAIL_PCT = float(getattr(config, "TRAIL_PCT", 4.0))
HARD_STOP_TRIGGER_PCT = float(getattr(config, "HARD_STOP_TRIGGER_PCT", -0.055))
HARD_STOP_POLL_INTERVAL = 30    # seconds

# ── timezone helper ───────────────────────────────────────────────────────────
_ET = ZoneInfo("America/New_York")


def _is_regular_market_hours() -> bool:
    """Returns True only during regular market hours 9:30 AM – 4:00 PM ET, Mon–Fri."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


# ── in-memory state ───────────────────────────────────────────────────────────
open_stops: dict[str, dict[str, str]] = {}
_exit_locks: set[str] = set()
_inactive_assets: set[str] = set()

# Symbols bought after-hours that need trailing stops at next market open.
# The bracket (hard stop + take profit) protects them overnight.
# At market open, attach_deferred_trailing_stops() cancels the bracket
# legs and replaces them with a trailing stop.
_DEFERRED_STOPS_FILE = pathlib.Path("deferred_stops.json")

def _load_deferred_stops() -> dict[str, float]:
    """Load deferred trailing stops from disk, surviving restarts."""
    if not _DEFERRED_STOPS_FILE.exists():
        return {}
    try:
        data = json.loads(_DEFERRED_STOPS_FILE.read_text())
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items()}
    except Exception:
        logger.warning("Failed to load deferred_stops.json — starting fresh")
    return {}

def _save_deferred_stops() -> None:
    """Persist deferred trailing stops to disk."""
    try:
        _DEFERRED_STOPS_FILE.write_text(json.dumps(_deferred_trailing_stops, indent=2))
    except Exception:
        logger.warning("Failed to save deferred_stops.json")

_deferred_trailing_stops: dict[str, float] = _load_deferred_stops()


def _headers() -> dict[str, str]:
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Alpaca API credentials")
    return {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
        "Content-Type": "application/json",
    }


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.request(method, url, headers=_headers(), **kwargs)


async def _get_account() -> Optional[dict]:
    try:
        resp = await _request("GET", f"{ALPACA_BASE_URL}/v2/account")
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
        logger.warning("Failed to fetch account: %s %s", resp.status_code, resp.text)
        return None
    except Exception:
        logger.exception("Failed to fetch account")
        return None


async def _get_open_positions() -> list[dict]:
    try:
        resp = await _request("GET", f"{ALPACA_BASE_URL}/v2/positions")
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        logger.warning("Failed to fetch open positions: %s %s", resp.status_code, resp.text)
        return []
    except Exception:
        logger.exception("Failed to fetch open positions")
        return []


async def _get_open_orders_for_symbol(symbol: str) -> list[dict]:
    try:
        resp = await _request("GET", f"{ALPACA_BASE_URL}/v2/orders", params={"status": "open", "symbols": symbol})
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        logger.warning("Failed to fetch open orders for %s: %s %s", symbol, resp.status_code, resp.text)
        return []
    except Exception:
        logger.exception("Failed to fetch open orders for %s", symbol)
        return []


async def _daily_loss_halt() -> bool:
    acct = await _get_account()
    if not acct:
        return False
    try:
        equity = float(acct.get("equity") or 0)
        last_equity = float(acct.get("last_equity") or 0)
        daily_pl = equity - last_equity
        return daily_pl <= DAILY_LOSS_LIMIT
    except Exception:
        logger.exception("Failed evaluating daily loss limit")
        return False


async def _position_exists_and_filled(symbol: str) -> bool:
    positions = await _get_open_positions()
    for pos in positions:
        if str(pos.get("symbol", "")).upper() != symbol.upper():
            continue
        try:
            qty = float(pos.get("qty") or 0)
            market_value = float(pos.get("market_value") or 0)
            return qty > 0 and market_value > 0
        except Exception:
            return False
    return False


async def _held_exit_qty(symbol: str) -> float:
    open_orders = await _get_open_orders_for_symbol(symbol)
    seen_groups: set[str] = set()
    held = 0.0
    standalone_held = 0.0

    for order in open_orders:
        if str(order.get("symbol", "")).upper() != symbol.upper():
            continue
        if str(order.get("side", "")).lower() != "sell":
            continue
        status = str(order.get("status", "")).lower()
        if status in {"filled", "canceled", "rejected", "expired"}:
            continue

        try:
            qty = float(order.get("qty") or order.get("remaining_qty") or order.get("filled_qty") or 0)
        except Exception:
            continue

        order_class = str(order.get("order_class", "")).lower()
        legs = order.get("legs") or []

        if order_class == "bracket" or legs:
            group_id = str(order.get("id", ""))
            if group_id and group_id not in seen_groups:
                seen_groups.add(group_id)
                held += qty
            continue

        parent_id = str(order.get("legs_parent_id") or order.get("parent_id") or "")
        if parent_id:
            if parent_id not in seen_groups:
                seen_groups.add(parent_id)
                held += qty
            continue

        standalone_held += qty

    return held + standalone_held


async def _available_exit_qty(symbol: str, requested_qty: float) -> float:
    if requested_qty <= 0:
        return 0.0
    if not await _position_exists_and_filled(symbol):
        return 0.0
    held = await _held_exit_qty(symbol)
    return max(0.0, requested_qty - held)


async def _buy_risk_check(symbol: str, qty: float, price: float) -> tuple[bool, str, float]:
    try:
        if qty <= 0:
            return False, "qty_nonpositive", 0.0
        if price <= 0:
            return False, "price_nonpositive", 0.0

        if await _daily_loss_halt():
            return False, "daily_loss_limit_hit", 0.0

        acct = await _get_account()
        if not acct:
            return False, "account_unavailable", 0.0

        buying_power = float(
            acct.get("buying_power")
            or acct.get("daytrading_buying_power")
            or acct.get("day_trade_buying_power")
            or 0
        )
        equity = float(acct.get("equity") or 0)

        if buying_power <= 0:
            return False, "no_buying_power", 0.0

        positions = await _get_open_positions()
        if len(positions) >= MAX_POSITIONS:
            return False, "max_positions_reached", 0.0

        gross_long_exposure = 0.0
        for pos in positions:
            try:
                mv = float(pos.get("market_value") or 0)
                if mv > 0:
                    gross_long_exposure += mv
            except Exception:
                continue

        if equity > 0 and MAX_LEVERAGE > 0 and gross_long_exposure > equity * MAX_LEVERAGE:
            return False, "gross_exposure_cap_reached", 0.0

        max_position_value = min(buying_power * POSITION_SIZE_PCT, MAX_POSITION_SIZE)
        if max_position_value <= 0:
            return False, "max_position_value_nonpositive", 0.0

        max_qty = int(max_position_value // price)
        if max_qty < 1:
            return False, "qty_too_small_for_cap", 0.0

        if buying_power < price * max_qty * BUYING_POWER_BUFFER:
            affordable_qty = int((buying_power / BUYING_POWER_BUFFER) // price)
            max_qty = min(max_qty, affordable_qty)

        if max_qty < 1:
            return False, "insufficient_buying_power", 0.0

        trimmed_qty = min(int(qty), max_qty)
        if trimmed_qty < 1:
            return False, "trimmed_qty_nonpositive", 0.0

        if trimmed_qty < int(qty):
            return True, "trimmed", float(trimmed_qty)

        return True, "ok", float(trimmed_qty)
    except Exception as exc:
        logger.exception("Buy risk check failed for %s", symbol)
        return False, str(exc), 0.0


async def _post_order(payload: dict) -> Optional[dict]:
    url = f"{ALPACA_BASE_URL}/v2/orders"
    retryable_statuses = {429, 500, 502, 503, 504}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(exclude_none=True)
            elif hasattr(payload, "dict"):
                payload = payload.dict(exclude_none=True)

            resp = await _request("POST", url, json=payload)

            if resp.status_code in (200, 201):
                return resp.json()

            if resp.status_code == 403:
                logger.warning("Non-retryable order rejection: %s %s", resp.status_code, resp.text)
                return None

            if resp.status_code not in retryable_statuses:
                logger.warning("Non-retryable order rejection: %s %s", resp.status_code, resp.text)
                if resp.status_code == 422 and "not active" in resp.text.lower():
                    symbol = str(payload.get("symbol", "")).upper()
                    if symbol:
                        _inactive_assets.add(symbol)
                        logger.warning("Blacklisting inactive asset %s for this session", symbol)
                return None

            logger.warning(
                "Retryable order failure (attempt %d/%d): %s %s",
                attempt, _MAX_RETRIES, resp.status_code, resp.text,
            )
        except httpx.TransportError as exc:
            logger.warning("Transport error on attempt %d/%d: %s", attempt, _MAX_RETRIES, exc)
        except Exception:
            logger.exception("Unexpected order error for payload=%s", payload)
            return None

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_DELAY * attempt)

    logger.error("All order attempts failed for payload: %s", payload)
    return None


async def _get_order_by_id(order_id: str) -> Optional[dict]:
    try:
        resp = await _request("GET", f"{ALPACA_BASE_URL}/v2/orders/{order_id}")
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Failed to fetch order %s: %s %s", order_id, resp.status_code, resp.text)
        return None
    except Exception:
        logger.exception("Failed to fetch order %s", order_id)
        return None


async def _cancel_order_by_id(order_id: str) -> None:
    try:
        resp = await _request("DELETE", f"{ALPACA_BASE_URL}/v2/orders/{order_id}")
        if resp.status_code not in (204, 200, 404, 422):
            logger.warning("Cancel order response for %s: %s %s", order_id, resp.status_code, resp.text)
    except Exception:
        logger.exception("Failed to cancel order %s", order_id)


async def _cancel_all_open_sell_orders(symbol: str) -> None:
    try:
        orders = await _get_open_orders_for_symbol(symbol)
        for order in orders:
            if str(order.get("symbol", "")).upper() != symbol.upper():
                continue
            if str(order.get("side", "")).lower() != "sell":
                continue
            status = str(order.get("status", "")).lower()
            if status in {"filled", "canceled", "rejected", "expired"}:
                continue
            oid = order.get("id")
            if oid:
                await _cancel_order_by_id(str(oid))

        for _ in range(_CANCEL_SETTLE_ATTEMPTS):
            remaining = await _get_open_orders_for_symbol(symbol)
            still_open = [
                o for o in remaining
                if str(o.get("symbol", "")).upper() == symbol.upper()
                and str(o.get("side", "")).lower() == "sell"
                and str(o.get("status", "")).lower() not in {"filled", "canceled", "rejected", "expired"}
            ]
            if not still_open:
                break
            await asyncio.sleep(_CANCEL_SETTLE_DELAY)
    except Exception:
        logger.exception("Failed to cancel open sell orders for %s", symbol)


async def cancel_other_stop(filled_symbol: str) -> None:
    if filled_symbol in open_stops:
        for oid in list(open_stops[filled_symbol].values()):
            if oid:
                try:
                    await _cancel_order_by_id(oid)
                except Exception:
                    pass
        del open_stops[filled_symbol]


async def _cancel_linked_exits(symbol: str) -> None:
    linked = open_stops.pop(symbol, {})
    for order_id in linked.values():
        try:
            await _cancel_order_by_id(order_id)
        except Exception:
            pass


async def place_market_buy(
    symbol: str,
    qty: float,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> Tuple[Optional[str], Optional[float]]:
    price = await get_latest_price(symbol)
    if not price:
        logger.warning("Skipping buy for %s: no price available", symbol)
        return None, None

    ok, reason, adjusted_qty = await _buy_risk_check(symbol, qty, price)
    if not ok:
        logger.warning("Skipping buy for %s qty=%s reason=%s", symbol, qty, reason)
        return None, None

    final_qty = int(adjusted_qty)
    if final_qty < 1:
        return None, None

    payload = {
        "symbol": symbol,
        "qty": str(final_qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }

    if take_profit_price is not None and stop_loss_price is not None:
        payload["order_class"] = "bracket"
        payload["take_profit"] = {
            "limit_price": str(round(take_profit_price, 2)),
            "time_in_force": "gtc",
        }
        payload["stop_loss"] = {
            "stop_price": str(round(stop_loss_price, 2)),
            "time_in_force": "gtc",
        }

    result = await _post_order(payload)
    if result:
        return result.get("id"), float(result.get("filled_avg_price") or 0) or None
    return None, None


async def place_take_profit_sell(symbol: str, qty: float, limit_price: float) -> Optional[str]:
    if qty <= 0 or not await _position_exists_and_filled(symbol):
        return None
    sell_qty = await _available_exit_qty(symbol, qty)
    if sell_qty <= 0:
        return None

    payload = {
        "symbol": symbol,
        "qty": str(sell_qty),
        "side": "sell",
        "type": "limit",
        "limit_price": str(round(limit_price, 2)),
        "time_in_force": "gtc",
    }

    result = await _post_order(payload)
    return result.get("id") if result else None


async def place_stop_loss_sell(symbol: str, qty: float, stop_price: float) -> Optional[str]:
    if qty <= 0 or not await _position_exists_and_filled(symbol):
        return None
    sell_qty = await _available_exit_qty(symbol, qty)
    if sell_qty <= 0:
        return None

    payload = {
        "symbol": symbol,
        "qty": str(sell_qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    }

    result = await _post_order(payload)
    if result:
        open_stops.setdefault(symbol, {})["stop"] = str(result.get("id"))
        return result.get("id")
    return None


async def place_trailing_stop_sell(symbol: str, qty: float, trail_percent: float) -> Optional[str]:
    if qty <= 0 or trail_percent <= 0 or not await _position_exists_and_filled(symbol):
        return None

    sell_qty = await _available_exit_qty(symbol, qty)
    if sell_qty <= 0:
        return None

    payload = TrailingStopOrderRequest(
        symbol=symbol,
        qty=sell_qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=float(trail_percent),
    )

    result = await _post_order(payload)
    if result:
        open_stops.setdefault(symbol, {})["trail"] = str(result.get("id"))
        return result.get("id")
    return None


async def place_market_sell(symbol: str, qty: float) -> Optional[str]:
    symbol = symbol.upper()
    if symbol in _inactive_assets:
        logger.debug("skip_inactive_asset symbol=%s", symbol)
        return None
    if qty <= 0:
        return None

    if symbol in _exit_locks:
        logger.warning("Skipping market sell for %s: exit locked", symbol)
        return None

    try:
        open_orders = await _get_open_orders_for_symbol(symbol)
        already_selling = any(
            str(o.get("side", "")).lower() == "sell"
            and str(o.get("status", "")).lower() in {"new", "accepted", "pending_new", "partially_filled"}
            for o in open_orders
        )
        if already_selling:
            logger.info("sell_skip_already_pending symbol=%s", symbol)
            return None
    except Exception:
        logger.exception("Failed to check open sell orders for %s", symbol)

    _exit_locks.add(symbol)
    try:
        await _cancel_all_open_sell_orders(symbol)
        await _cancel_linked_exits(symbol)

        available_qty = await _available_exit_qty(symbol, qty)
        if available_qty <= 0:
            positions = await _get_open_positions()
            for pos in positions:
                if str(pos.get("symbol", "")).upper() == symbol.upper():
                    try:
                        available_qty = float(pos.get("qty") or 0)
                    except Exception:
                        available_qty = 0.0
                    break

        if available_qty <= 0:
            logger.warning("Skipping market sell for %s: no available qty after cancel", symbol)
            return None

        payload = {
            "symbol": symbol,
            "qty": str(available_qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }

        result = await _post_order(payload)
        return result.get("id") if result else None
    finally:
        _exit_locks.discard(symbol)


async def place_bracket_buy(
    symbol: str,
    qty: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    use_trailing: bool = True,
    trailing_stop_pct: float = 4.0,
) -> Tuple[Optional[str], Optional[float]]:
    estimated_price = await get_latest_price(symbol)
    if not estimated_price:
        logger.warning("Skipping bracket buy for %s: no price available", symbol)
        return None, None

    # Add 0.5% buffer to account for price movement between estimate and fill
    take_profit_price = round(estimated_price * (1.0 + take_profit_pct + 0.005), 2)
    take_profit_price = max(take_profit_price, round(estimated_price + 0.10, 2))

    stop_loss_price = round(estimated_price * (1.0 - stop_loss_pct - 0.005), 2)
    stop_loss_price = min(stop_loss_price, round(estimated_price - 0.10, 2))

    # Place plain market buy — no bracket legs
    # Trailing stop will be attached after fill (more reliable than bracket)
    order_id, filled_price = await place_market_buy(
        symbol, qty,
        take_profit_price=None,
        stop_loss_price=None,
    )
    if not order_id:
        return None, None

    # Poll for fill
    filled_qty = 0.0
    final_order = None
    for _ in range(_FILL_POLL_ATTEMPTS):
        order = await _get_order_by_id(order_id)
        if order:
            final_order = order
            status = str(order.get("status", "")).lower()
            filled_price = float(order.get("filled_avg_price") or 0) or filled_price
            filled_qty = float(order.get("filled_qty") or 0)
            if status in {"filled", "partially_filled"} and filled_qty > 0:
                break
        await asyncio.sleep(_FILL_POLL_DELAY)

    if not filled_price or filled_price <= 0:
        filled_price = estimated_price

    if not filled_qty or filled_qty <= 0:
        if final_order:
            filled_qty = float(final_order.get("filled_qty") or 0)
        if not filled_qty:
            filled_qty = qty

    # ── exit protection ───────────────────────────────────────────────────────
    trail_id = None
    _in_regular_hours = _is_regular_market_hours()

    if use_trailing and _in_regular_hours:
        # Regular hours: cancel bracket legs and replace with trailing stop.
        # The trailing stop lets winners run and locks in gains on pullback.
        try:
            child_orders = await _get_open_orders_for_symbol(symbol)
            for child in child_orders:
                child_status = str(child.get("status", "")).lower()
                child_side = str(child.get("side", "")).lower()
                if (
                    child_side == "sell"
                    and child_status not in {"filled", "canceled", "rejected", "expired"}
                ):
                    child_id = child.get("id")
                    if child_id:
                        await _cancel_order_by_id(str(child_id))

            # Wait for both legs to fully settle
            settled = False
            for _ in range(_CANCEL_SETTLE_ATTEMPTS):
                await asyncio.sleep(_CANCEL_SETTLE_DELAY)
                remaining = await _get_open_orders_for_symbol(symbol)
                still_open = [
                    o for o in remaining
                    if str(o.get("side", "")).lower() == "sell"
                    and str(o.get("status", "")).lower()
                    not in {"filled", "canceled", "rejected", "expired"}
                ]
                if not still_open:
                    settled = True
                    break

            if not settled:
                logger.warning(
                    "Bracket legs for %s did not settle after %.0fs — "
                    "bracket stays active, trailing stop queued for market open",
                    symbol, _CANCEL_SETTLE_ATTEMPTS * _CANCEL_SETTLE_DELAY,
                )
                if use_trailing and trailing_stop_pct > 0:
                    _deferred_trailing_stops[symbol] = trailing_stop_pct
                    _save_deferred_stops()
            else:
                trail_id = await place_trailing_stop_sell(symbol, filled_qty, trailing_stop_pct)
                if trail_id:
                    logger.info(
                        "Regular hours buy %s — trailing stop attached trail=%.1f%% id=%s",
                        symbol, trailing_stop_pct, trail_id,
                    )
                else:
                    logger.warning(
                        "Trailing stop placement failed for %s — queued for market open",
                        symbol,
                    )
                    if use_trailing and trailing_stop_pct > 0:
                        _deferred_trailing_stops[symbol] = trailing_stop_pct
                        _save_deferred_stops()
        except Exception:
            logger.exception("Failed to attach trailing stop for %s", symbol)

    else:
        # After hours: bracket legs (hard stop + take profit) stay active overnight.
        # Queue a deferred trailing stop — at market open, attach_deferred_trailing_stops()
        # will cancel the bracket legs and replace them with a trailing stop.
        if use_trailing and trailing_stop_pct > 0:
            _deferred_trailing_stops[symbol] = trailing_stop_pct
            _save_deferred_stops()
        logger.info(
            "After-hours buy %s — bracket exits active (tp=%.2f sl=%.2f), "
            "trailing stop queued for market open",
            symbol, take_profit_price, stop_loss_price,
        )
    # ── end exit protection ───────────────────────────────────────────────────

    open_stops[symbol] = {
        "tp": "",
        "stop": "",
        "trail": trail_id or "",
    }

    logger.info(
        "place_bracket_buy complete: %s entry=%.2f tp=%.2f sl=%.2f trail=%s in_hours=%s settled=%s",
        symbol, filled_price, take_profit_price, stop_loss_price, trail_id, _in_regular_hours,
        settled if _in_regular_hours else "n/a",
    )

    return order_id, filled_price

async def place_market_short(
    symbol: str,
    qty: float,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> Tuple[Optional[str], Optional[float]]:
    """Place a market sell-short order with optional bracket legs."""
    price = await get_latest_price(symbol)
    if not price:
        logger.warning("Skipping short for %s: no price available", symbol)
        return None, None

    ok, reason, adjusted_qty = await _short_risk_check(symbol, qty, price)
    if not ok:
        logger.warning("Skipping short for %s qty=%s reason=%s", symbol, qty, reason)
        return None, None

    final_qty = int(adjusted_qty)
    if final_qty < 1:
        return None, None

    payload = {
        "symbol": symbol,
        "qty": str(final_qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }

    if take_profit_price is not None and stop_loss_price is not None:
        payload["order_class"] = "bracket"
        payload["take_profit"] = {
            "limit_price": str(round(take_profit_price, 2)),
            "time_in_force": "gtc",
        }
        payload["stop_loss"] = {
            "stop_price": str(round(stop_loss_price, 2)),
            "time_in_force": "gtc",
        }

    result = await _post_order(payload)
    if result:
        return result.get("id"), float(result.get("filled_avg_price") or 0) or None
    return None, None


async def place_trailing_stop_buy(symbol: str, qty: float, trail_percent: float) -> Optional[str]:
    """Place a trailing stop BUY to cover a short position."""
    if qty <= 0 or trail_percent <= 0:
        return None
    payload = TrailingStopOrderRequest(
        symbol=symbol,
        qty=abs(qty),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        trail_percent=float(trail_percent),
    )

    result = await _post_order(payload)
    if result:
        open_stops.setdefault(symbol, {})["trail"] = str(result.get("id"))
        return result.get("id")
    return None


async def _short_risk_check(symbol: str, qty: float, price: float) -> tuple[bool, str, float]:
    """Risk checks specific to short positions."""
    try:
        if qty <= 0:
            return False, "qty_nonpositive", 0.0
        if price <= 0:
            return False, "price_nonpositive", 0.0
        if price < 10.0:
            return False, "price_below_short_minimum", 0.0

        if await _daily_loss_halt():
            return False, "daily_loss_limit_hit", 0.0

        acct = await _get_account()
        if not acct:
            return False, "account_unavailable", 0.0

        equity = float(acct.get("equity") or 0)
        buying_power = float(acct.get("buying_power") or 0)

        if buying_power <= 0:
            return False, "no_buying_power", 0.0

        # Check short exposure cap (0.5x equity)
        MAX_SHORT_LEVERAGE = 0.5
        positions = await _get_open_positions()
        gross_short_exposure = 0.0
        for pos in positions:
            try:
                qty_pos = float(pos.get("qty", 0))
                mv = float(pos.get("market_value", 0))
                if qty_pos < 0:  # short position
                    gross_short_exposure += abs(mv)
            except Exception:
                continue

        if equity > 0 and gross_short_exposure >= equity * MAX_SHORT_LEVERAGE:
            return False, "short_exposure_cap_reached", 0.0

        # Max short position size same as long
        max_position_value = min(buying_power * POSITION_SIZE_PCT, MAX_POSITION_SIZE)
        if max_position_value <= 0:
            return False, "max_position_value_nonpositive", 0.0

        max_qty = int(max_position_value // price)
        if max_qty < 1:
            return False, "qty_too_small_for_cap", 0.0

        trimmed_qty = min(int(qty), max_qty)
        if trimmed_qty < 1:
            return False, "trimmed_qty_nonpositive", 0.0

        return True, "ok", float(trimmed_qty)

    except Exception as exc:
        logger.exception("Short risk check failed for %s", symbol)
        return False, str(exc), 0.0


async def place_bracket_short(
    symbol: str,
    qty: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    use_trailing: bool = True,
    trailing_stop_pct: float = 4.0,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Open a short position with bracket protection.

    For shorts:
    - take_profit is BELOW entry (price falls = profit)
    - stop_loss is ABOVE entry (price rises = loss)
    - trailing stop is a BUY order that trails the falling price
    """
    estimated_price = await get_latest_price(symbol)
    if not estimated_price:
        logger.warning("Skipping bracket short for %s: no price available", symbol)
        return None, None

    # For shorts: TP is below entry, SL is above entry
    take_profit_price = round(estimated_price * (1.0 - take_profit_pct), 2)
    take_profit_price = min(take_profit_price, round(estimated_price - 0.05, 2))

    stop_loss_price = round(estimated_price * (1.0 + stop_loss_pct), 2)
    stop_loss_price = max(stop_loss_price, round(estimated_price + 0.05, 2))

    order_id, filled_price = await place_market_short(
        symbol, qty,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
    )
    if not order_id:
        return None, None

    # Poll for fill
    filled_qty = 0.0
    final_order = None
    for _ in range(_FILL_POLL_ATTEMPTS):
        order = await _get_order_by_id(order_id)
        if order:
            final_order = order
            status = str(order.get("status", "")).lower()
            filled_price = float(order.get("filled_avg_price") or 0) or filled_price
            filled_qty = float(order.get("filled_qty") or 0)
            if status in {"filled", "partially_filled"} and filled_qty > 0:
                break
        await asyncio.sleep(_FILL_POLL_DELAY)

    if not filled_price or filled_price <= 0:
        filled_price = estimated_price
    if not filled_qty or filled_qty <= 0:
        if final_order:
            filled_qty = float(final_order.get("filled_qty") or 0)
        if not filled_qty:
            filled_qty = qty

    # Exit protection — trailing stop buy
    trail_id = None
    _in_regular_hours = _is_regular_market_hours()

    if use_trailing and _in_regular_hours:
        try:
            # Wait for position to settle before cancelling bracket legs
            await asyncio.sleep(2.0)
            # Cancel bracket legs
            child_orders = await _get_open_orders_for_symbol(symbol)
            for child in child_orders:
                child_status = str(child.get("status", "")).lower()
                child_side = str(child.get("side", "")).lower()
                if (
                    child_side == "buy"
                    and child_status not in {"filled", "canceled", "rejected", "expired"}
                ):
                    child_id = child.get("id")
                    if child_id:
                        await _cancel_order_by_id(str(child_id))

            # Wait for legs to settle
            settled = False
            for _ in range(_CANCEL_SETTLE_ATTEMPTS):
                await asyncio.sleep(_CANCEL_SETTLE_DELAY)
                remaining = await _get_open_orders_for_symbol(symbol)
                still_open = [
                    o for o in remaining
                    if str(o.get("side", "")).lower() == "buy"
                    and str(o.get("status", "")).lower()
                    not in {"filled", "canceled", "rejected", "expired"}
                ]
                if not still_open:
                    settled = True
                    break

            if not settled:
                logger.warning(
                    "Short bracket legs for %s did not settle — keeping bracket active",
                    symbol,
                )
            else:
                # Retry trailing stop placement up to 3 times
                for attempt in range(3):
                    trail_id = await place_trailing_stop_buy(symbol, filled_qty, trailing_stop_pct)
                    if trail_id:
                        logger.info(
                            "Short %s — trailing stop buy attached trail=%.1f%% id=%s attempt=%d",
                            symbol, trailing_stop_pct, trail_id, attempt + 1,
                        )
                        break
                    logger.warning("Trailing stop attempt %d failed for %s — retrying in 3s", attempt + 1, symbol)
                    await asyncio.sleep(3.0)
                if not trail_id:
                    logger.warning("Trailing stop buy failed for short %s — placing hard stop loss as fallback", symbol)
                    # Fallback: place hard stop loss above entry to protect short
                    fallback_stop = round(stop_loss_price, 2)
                    fallback_payload = {
                        "symbol": symbol,
                        "qty": str(int(filled_qty)),
                        "side": "buy",
                        "type": "stop",
                        "time_in_force": "gtc",
                        "stop_price": str(fallback_stop),
                    }
                    try:
                        fallback_result = await _post_order(fallback_payload)
                        if fallback_result:
                            logger.info("Short %s — fallback stop loss placed at %.2f", symbol, fallback_stop)
                        else:
                            logger.error("Short %s — UNPROTECTED: both trailing stop and fallback stop failed", symbol)
                    except Exception:
                        logger.exception("Short %s — fallback stop loss placement failed", symbol)

        except Exception:
            logger.exception("Failed to attach trailing stop buy for short %s", symbol)
    else:
        logger.info(
            "Short %s — bracket exits active (tp=%.2f sl=%.2f) trail queued",
            symbol, take_profit_price, stop_loss_price,
        )

    open_stops[symbol] = {
        "tp": "",
        "stop": "",
        "trail": trail_id or "",
    }

    logger.info(
        "place_bracket_short complete: %s entry=%.2f tp=%.2f sl=%.2f trail=%s in_hours=%s",
        symbol, filled_price, take_profit_price, stop_loss_price, trail_id, _in_regular_hours,
    )

    return order_id, filled_price

async def attach_deferred_trailing_stops() -> None:
    """
    Called once at regular market open each day.

    For every symbol bought after-hours (which has a bracket order protecting it):
    1. Cancel the bracket legs (hard stop + take profit)
    2. Attach a GTC trailing stop

    This upgrades after-hours positions from static bracket protection
    to dynamic trailing stop protection once the market opens.
    """
    if not _deferred_trailing_stops:
        return

    # Strict window: 9:31 AM – 3:55 PM ET, Mon–Fri only
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return
    safe_open  = now.replace(hour=9,  minute=31, second=0, microsecond=0)
    safe_close = now.replace(hour=15, minute=55, second=0, microsecond=0)
    if not (safe_open <= now < safe_close):
        logger.info(
            "attach_deferred_trailing_stops: outside safe window (%s ET) — skipping",
            now.strftime("%H:%M"),
        )
        return

    symbols = list(_deferred_trailing_stops.keys())
    logger.info(
        "Upgrading %d after-hours position(s) from bracket to trailing stop: %s",
        len(symbols), symbols,
    )

    for symbol in symbols:
        trail_pct = _deferred_trailing_stops.get(symbol, TRAIL_PCT)
        try:
            # Step 1: cancel the bracket legs (hard stop + take profit)
            await _cancel_all_open_sell_orders(symbol)

            # Step 2: attach trailing stop (qty=999999 is capped to actual position size internally)
            # Get actual position qty instead of using 999999
            positions = await _get_open_positions()
            pos = next((p for p in positions if p.get("symbol") == symbol), None)
            actual_qty = float(pos.get("qty", 0)) if pos else 0
            if actual_qty <= 0:
                logger.warning("No position found for %s — skipping deferred stop", symbol)
                _deferred_trailing_stops.pop(symbol, None)
                _save_deferred_stops()
                continue

            trail_id = await place_trailing_stop_sell(symbol, actual_qty, trail_pct)
            if trail_id:
                logger.info(
                    "Deferred trailing stop attached for %s trail=%.1f%% id=%s",
                    symbol, trail_pct, trail_id,
                )
                _deferred_trailing_stops.pop(symbol, None)
                _save_deferred_stops()
            else:
                logger.warning(
                    "Failed to attach deferred trailing stop for %s — bracket may "
                    "have already filled, or position no longer exists. Will retry next cycle.",
                    symbol,
                )
        except Exception:
            logger.exception("Error upgrading %s from bracket to trailing stop", symbol)
        await asyncio.sleep(0.5)

async def monitor_position_protection(interval_seconds: int = 7200) -> None:
    """
    Background task — runs every 2 hours during market hours.
    Ensures every open position has an active trailing stop or bracket leg.
    If a position has no protection, attaches a 4% trailing stop.
    """
    logger.info("Position protection monitor started — interval=%dh", interval_seconds // 3600)
    while True:
        await asyncio.sleep(interval_seconds)
        if not _is_regular_market_hours():
            continue
        try:
            positions = await _get_open_positions()
            if not positions:
                continue

            logger.info("Protection sweep — checking %d positions", len(positions))
            unprotected = []

            for p in positions:
                symbol = p.get("symbol")
                qty = float(p.get("qty", 0))
                if not symbol or qty <= 0 or symbol == "CXE":
                    continue

                # Check for any active sell orders
                orders = await _get_open_orders_for_symbol(symbol)
                active_sells = [
                    o for o in orders
                    if str(o.get("side", "")).lower() == "sell"
                    and str(o.get("status", "")).lower()
                    not in {"filled", "canceled", "rejected", "expired"}
                ]

                if not active_sells:
                    unprotected.append((symbol, qty))

            if not unprotected:
                logger.info("Protection sweep — all %d positions protected ✓", len(positions))
                continue

            logger.warning(
                "Protection sweep — %d unprotected positions found: %s",
                len(unprotected), [s for s, _ in unprotected]
            )

            for symbol, qty in unprotected:
                try:
                    trail_id = await place_trailing_stop_sell(symbol, qty, TRAIL_PCT)
                    if trail_id:
                        logger.info(
                            "Protection sweep — trailing stop attached for %s qty=%.0f trail=%.1f%%",
                            symbol, qty, TRAIL_PCT,
                        )
                    else:
                        logger.warning(
                            "Protection sweep — failed to attach trailing stop for %s",
                            symbol,
                        )
                except Exception:
                    logger.exception("Protection sweep — error attaching stop for %s", symbol)
                await asyncio.sleep(2.0)  # rate limit buffer

        except Exception:
            logger.exception("Protection sweep failed")

async def monitor_hard_stops(poll_interval: int = HARD_STOP_POLL_INTERVAL):
    """
    Background loop: polls every 30s.
    If any position's unrealized P&L% hits -6%, cancel all exit orders
    and market-sell immediately — regardless of what other exits are active.
    Works for both regular-hours (trailing stop) and after-hours (bracket) positions.
    """
    logger.info(
        "Hard stop monitor started — trigger=%.0f%% poll=%ds",
        HARD_STOP_TRIGGER_PCT * 100, poll_interval,
    )
    while True:
        try:
            positions = await _get_open_positions()
            for pos in positions:
                symbol = pos.get("symbol")
                try:
                    plpc = float(pos.get("unrealized_plpc") or 0)
                except Exception:
                    continue

                # For long positions: trigger if down -6%
                # For short positions: trigger if up +6% (price rising = loss)
                qty = float(pos.get("qty") or 0)
                is_short = qty < 0

                if (not is_short and plpc <= HARD_STOP_TRIGGER_PCT) or \
                   (is_short and plpc >= abs(HARD_STOP_TRIGGER_PCT)):
                    logger.warning(
                        "Hard stop trigger for %s: unrealized_plpc=%.4f trigger=%.4f short=%s",
                        symbol, plpc, HARD_STOP_TRIGGER_PCT, is_short,
                    )
                    await _cancel_all_open_sell_orders(symbol)
                    await _cancel_linked_exits(symbol)
                    qty = float(pos.get("qty") or 0)
                    if not is_short and qty > 0:
                        # Long position — market sell
                        sell_payload = {
                            "symbol": symbol,
                            "qty": str(int(qty)),
                            "side": "sell",
                            "type": "market",
                            "time_in_force": "day",
                        }
                        result = await _post_order(sell_payload)
                        if result:
                            logger.info(
                                "Hard stop executed for long %s: sold %s shares",
                                symbol, qty,
                            )
                    elif is_short and qty < 0:
                        # Short position — market buy to cover
                        cover_payload = {
                            "symbol": symbol,
                            "qty": str(int(abs(qty))),
                            "side": "buy",
                            "type": "market",
                            "time_in_force": "day",
                        }
                        result = await _post_order(cover_payload)
                        if result:
                            logger.info(
                                "Hard stop executed for short %s: covered %s shares",
                                symbol, abs(qty),
                            )
                    open_stops.pop(symbol, None)
                    _deferred_trailing_stops.pop(symbol, None)
                    _save_deferred_stops()

        except Exception:
            logger.exception("Error in monitor_hard_stops loop")

        await asyncio.sleep(poll_interval)


async def get_latest_price(symbol: str) -> Optional[float]:
    try:
        bar = await get_latest_bar(symbol, timeframe="1Min")
        if not bar:
            return None
        for key in ("c", "close"):
            if bar.get(key) is not None:
                return float(bar[key])
        return None
    except Exception:
        logger.exception("execution: failed to get latest price for %s", symbol)
        return None


def calculate_qty(price: float, position_size_usd: float, buying_power: float) -> float:
    if price <= 0 or buying_power <= 0:
        return 0.0

    max_dollar = min(
        position_size_usd,
        MAX_POSITION_SIZE,
        buying_power / BUYING_POWER_BUFFER,
        buying_power * POSITION_SIZE_PCT,
    )

    qty = int(max_dollar // price)
    return float(max(0, qty))
