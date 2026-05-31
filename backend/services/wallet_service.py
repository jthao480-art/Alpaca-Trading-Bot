from typing import Optional
import requests
from .auth import alpaca_request_async, get_alpaca_base_url, get_alpaca_headers

async def get_account() -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", "/v2/account")
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized. Headers: {get_alpaca_headers()}, URL: {get_alpaca_base_url()}")
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching account: {e}")
        return None

async def get_portfolio_history(timeframe: str = "1D") -> Optional[dict]:
    try:
        response = await alpaca_request_async(
            "GET",
            "/v2/account/portfolio/history",
            params={"timeframe": timeframe},
        )
        if response.status_code == 401:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error: {e}")
        return None

async def get_portfolio_health() -> float:
    try:
        account = await get_account()
        if not account:
            return 0.0

        equity = float(account.get("equity", 0) or 0)
        buying_power = float(account.get("buying_power", 0) or 0)
        cash = float(account.get("cash", 0) or 0)

        if equity <= 0:
            return 0.0

        score = min(
            1.0,
            max(
                0.0,
                (equity / max(equity, 1.0)) * 0.5
                + (buying_power / max(equity, 1.0)) * 0.25
                + (cash / max(equity, 1.0)) * 0.25,
            ),
        )
        return round(score, 3)
    except Exception as e:
        print(f"Error: {e}")
        return 0.0

async def list_positions() -> list:
    try:
        response = await alpaca_request_async("GET", "/v2/positions")
        if response.status_code == 401:
            return []
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error: {e}")
        return []

async def get_position(symbol: str) -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", f"/v2/positions/{symbol}")
        if response.status_code in [401, 404]:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error: {e}")
        return None

async def close_position(symbol: str, qty: Optional[float] = None, side: str = "sell") -> Optional[dict]:
    try:
        response = await alpaca_request_async(
            "DELETE",
            f"/v2/positions/{symbol}",
            data={"qty": qty, "side": side} if qty else {"side": side},
        )
        if response.status_code == 401:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error: {e}")
        return None