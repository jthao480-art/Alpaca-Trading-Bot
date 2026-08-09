import asyncio
from backend.execution import _request

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

async def add_stops():
    shorts = [
        {'symbol': 'FIW',  'qty': 12,  'entry': 109.42},
        {'symbol': 'GRMN', 'qty': 12,  'entry': 236.19},
        {'symbol': 'O',    'qty': 17,  'entry': 62.11},
    ]
    
    for s in shorts:
        sym = s['symbol']
        qty = s['qty']
        stop_price = round(s['entry'] * 1.02, 2)  # 2% above entry = stop loss
        
        order = {
            "symbol": sym,
            "qty": str(qty),
            "side": "buy",
            "type": "stop",
            "time_in_force": "gtc",
            "stop_price": str(stop_price),
        }
        try:
            result = await _request("POST", f"{ALPACA_BASE_URL}/v2/orders", json=order)
            print(f'{sym}: stop loss placed at {stop_price} (entry={s["entry"]})')
        except Exception as e:
            print(f'{sym}: ERROR - {e}')

asyncio.run(add_stops())
