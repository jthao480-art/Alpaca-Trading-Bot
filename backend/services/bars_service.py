from typing import Optional, List
from datetime import datetime, timedelta
from .auth import alpaca_request_async, get_alpaca_data_feed

async def get_bars(symbol: str, timeframe: str = "1Min", start: Optional[datetime] = None, end: Optional[datetime] = None, limit: int = 1000) -> Optional[List[dict]]:
    try:
        if start is None:
            start = datetime.utcnow() - timedelta(days=30)
        if end is None:
            end = datetime.utcnow()
        params = {
            "timeframe": timeframe,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "feed": get_alpaca_data_feed(),
            "sort": "asc",
        }
        response = await alpaca_request_async("GET", f"/v2/stocks/{symbol}/bars", params=params, use_data_api=True)
        if response.status_code == 401:
            print("ERROR: 401 Unauthorized from Alpaca data API.")
            return None
        response.raise_for_status()
        return response.json().get("bars", [])
    except Exception as e:
        print(f"Error fetching bars for {symbol}: {e}")
        return None

async def get_latest_bar(symbol: str, timeframe: str = "1Min") -> Optional[dict]:
    bars = await get_bars(symbol, timeframe, limit=1)
    return bars[0] if bars else None

async def get_recent_bars(symbol: str, timeframe: str = "1Min", count: int = 100) -> Optional[List[dict]]:
    return await get_bars(symbol, timeframe, start=datetime.utcnow() - timedelta(days=7), end=datetime.utcnow(), limit=count)

async def get_latest_quote(symbol: str) -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", f"/v1beta1/stocks/{symbol}/snapshot", use_data_api=True)
        if response.status_code == 401:
            return None
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("latest_quote")
    except Exception as e:
        print(f"Error fetching quote for {symbol}: {e}")
        return None

async def get_latest_trade(symbol: str) -> Optional[dict]:
    try:
        response = await alpaca_request_async("GET", f"/v1beta1/stocks/{symbol}/snapshot", use_data_api=True)
        if response.status_code in [401, 404]:
            return None
        response.raise_for_status()
        return response.json().get("latest_trade")
    except Exception as e:
        print(f"Error fetching trade for {symbol}: {e}")
        return None