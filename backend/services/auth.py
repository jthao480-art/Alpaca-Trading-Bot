from __future__ import annotations

from typing import Any
import httpx

from backend import config


def get_alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY or "",
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY or config.ALPACA_API_SECRET_KEY or "",
    }


def get_alpaca_data_feed() -> str:
    return "iex"


def _get_base_url(use_data_api: bool = False) -> str:
    return config.ALPACA_DATA_URL if use_data_api else config.ALPACA_BASE_URL


async def alpaca_request_async(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    use_data_api: bool = False,
):
    base_url = _get_base_url(use_data_api=use_data_api).rstrip("/")
    path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{path}"

    headers = get_alpaca_headers()
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        return await client.request(method, url, params=params)