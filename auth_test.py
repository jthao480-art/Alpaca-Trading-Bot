from __future__ import annotations

import asyncio

from backend import config
from backend.services.auth import (
    get_alpaca_headers,
    get_alpaca_base_url,
    get_alpaca_data_base_url,
    alpaca_request_async,
)


def print_env() -> None:
    print("ALPACA_API_KEY:", bool(config.ALPACA_API_KEY))
    print("ALPACA_API_SECRET_KEY:", bool(config.ALPACA_API_SECRET_KEY))
    print("ALPACA_BASE_URL:", config.ALPACA_BASE_URL)
    print("BASE_URL:", get_alpaca_base_url())
    print("DATA_BASE_URL:", get_alpaca_data_base_url())
    print("HEADERS:", get_alpaca_headers())


async def main() -> None:
    print_env()
    resp = await alpaca_request_async("GET", "/v2/account")
    print("STATUS:", resp.status_code)
    print(resp.text[:500])


if __name__ == "__main__":
    asyncio.run(main())
