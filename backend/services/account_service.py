from __future__ import annotations

import logging

from alpaca.trading.client import TradingClient

from backend import config

logger = logging.getLogger(__name__)


def get_trading_client() -> TradingClient:
    return TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )


def get_account():
    client = get_trading_client()
    return client.get_account()


def get_account_buying_power() -> float:
    try:
        account = get_account()
        return max(0.0, float(getattr(account, "buying_power", 0) or 0))
    except Exception:
        logger.exception("Failed to fetch account buying power")
        return 0.0


def get_account_cash() -> float:
    try:
        account = get_account()
        return max(0.0, float(getattr(account, "cash", 0) or 0))
    except Exception:
        logger.exception("Failed to fetch account cash")
        return 0.0


def get_account_equity() -> float:
    try:
        account = get_account()
        return max(0.0, float(getattr(account, "equity", 0) or 0))
    except Exception:
        logger.exception("Failed to fetch account equity")
        return 0.0


def get_daytrading_buying_power() -> float:
    try:
        account = get_account()
        return max(0.0, float(getattr(account, "daytrading_buying_power", 0) or 0))
    except Exception:
        logger.exception("Failed to fetch day trading buying power")
        return 0.0
