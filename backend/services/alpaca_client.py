from __future__ import annotations

from backend import config


def get_alpaca_base_url() -> str:
    return config.ALPACA_BASE_URL


def get_alpaca_data_base_url() -> str:
    return config.ALPACA_DATA_URL