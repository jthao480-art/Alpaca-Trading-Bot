from typing import Dict, Optional
import requests
from .auth import alpaca_request, get_alpaca_base_url, get_alpaca_headers

def get_account() -> Optional[dict]:
    """
    Get account information from Alpaca.
    Returns account data or None if request fails.
    """
    try:
        response = alpaca_request("GET", "/v2/account")
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca. Check your API credentials.")
            print(f"Headers being used: {get_alpaca_headers()}")
            print(f"Base URL: {get_alpaca_base_url()}")
            return None
        
        response.raise_for_status()
        return response.json()
    
    except requests.RequestException as e:
        print(f"Error fetching account: {e}")
        return None

def get_portfolio_history(timeframe: str = "1D") -> Optional[dict]:
    """
    Get portfolio history.
    """
    try:
        params = {"timeframe": timeframe}
        response = alpaca_request("GET", "/v2/account/portfolio/history", params=params)
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca. Check your API credentials.")
            return None
        
        response.raise_for_status()
        return response.json()
    
    except requests.RequestException as e:
        print(f"Error fetching portfolio history: {e}")
        return None

def list_positions() -> list:
    """
    Get all open positions.
    Returns empty list if request fails.
    """
    try:
        response = alpaca_request("GET", "/v2/positions")
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca. Check your API credentials.")
            return []
        
        response.raise_for_status()
        return response.json()
    
    except requests.RequestException as e:
        print(f"Error fetching positions: {e}")
        return []

def get_position(symbol: str) -> Optional[dict]:
    """
    Get a specific position by symbol.
    Returns None if not found or on error.
    """
    try:
        response = alpaca_request("GET", f"/v2/positions/{symbol}")
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca. Check your API credentials.")
            return None
        
        if response.status_code == 404:
            return None
        
        response.raise_for_status()
        return response.json()
    
    except requests.RequestException as e:
        print(f"Error fetching position {symbol}: {e}")
        return None

def close_position(symbol: str, qty: Optional[float] = None, side: str = "sell") -> Optional[dict]:
    """
    Close a position.
    """
    try:
        data = {}
        if qty:
            data["qty"] = qty
        if side:
            data["side"] = side
        
        response = alpaca_request("DELETE", f"/v2/positions/{symbol}", data=data)
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca. Check your API credentials.")
            return None
        
        response.raise_for_status()
        return response.json()
    
    except requests.RequestException as e:
        print(f"Error closing position {symbol}: {e}")
        return None