from typing import Optional, Dict
from .auth import get_alpaca_headers, get_alpaca_base_url, alpaca_request

def test_connection() -> bool:
    """Test Alpaca API connection."""
    try:
        response = alpaca_request("GET", "/v2/account")
        
        if response.status_code == 401:
            print(f"ERROR: 401 Unauthorized from Alpaca.")
            print(f"Headers: {get_alpaca_headers()}")
            print(f"Base URL: {get_alpaca_base_url()}")
            return False
        
        response.raise_for_status()
        account = response.json()
        print(f"Connected to Alpaca. Account ID: {account.get('id')}")
        return True
    
    except Exception as e:
        print(f"Error connecting to Alpaca: {e}")
        return False