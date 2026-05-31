from pathlib import Path
import os
import asyncio
import requests
from typing import Dict, Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=True)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_base_url(url: Optional[str], default: str) -> str:
    value = _clean(url) or default
    return value.rstrip("/")


def get_alpaca_headers() -> Dict[str, str]:
    api_key = _clean(os.getenv("APCA_API_KEY_ID"))
    secret_key = _clean(os.getenv("APCA_API_SECRET_KEY"))

    if not api_key or not secret_key:
        raise ValueError(
            "Missing Alpaca credentials. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env."
        )

    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_alpaca_base_url() -> str:
    return _normalize_base_url(
        os.getenv("ALPACA_BASE_URL") or os.getenv("APCA_API_BASE_URL"),
        "https://paper-api.alpaca.markets",
    )


def get_alpaca_data_base_url() -> str:
    return _normalize_base_url(
        os.getenv("ALPACA_DATA_BASE_URL"),
        "https://data.alpaca.markets",
    )


def get_alpaca_data_feed() -> str:
    return _clean(os.getenv("ALPACA_DATA_FEED")) or "iex"


def alpaca_request(
    method: str,
    endpoint: str,
    data: Optional[dict] = None,
    params: Optional[dict] = None,
    use_data_api: bool = False,
) -> requests.Response:
    headers = get_alpaca_headers()
    base_url = get_alpaca_data_base_url() if use_data_api else get_alpaca_base_url()
    url = f"{base_url}{endpoint if endpoint.startswith('/') else '/' + endpoint}"
    return requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        json=data if data else None,
        params=params,
        timeout=30,
    )


async def alpaca_request_async(
    method: str,
    endpoint: str,
    data: Optional[dict] = None,
    params: Optional[dict] = None,
    use_data_api: bool = False,
) -> requests.Response:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: alpaca_request(method, endpoint, data, params, use_data_api),
    )