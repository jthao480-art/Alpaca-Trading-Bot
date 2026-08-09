import asyncio
from backend.execution import _get_open_positions

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

async def close_all_shorts():
    from backend.execution import _request
    positions = await _get_open_positions()
    shorts = [p for p in positions if float(p.get('qty', 0)) < 0]
    print(f'Closing {len(shorts)} shorts...')
    
    for p in shorts:
        sym = p.get('symbol')
        qty = abs(float(p.get('qty', 0)))
        pnl = float(p.get('unrealized_pl', 0))
        try:
            result = await _request("DELETE", f"{ALPACA_BASE_URL}/v2/positions/{sym}")
            print(f'Closed {sym} pnl={pnl:.2f}')
        except Exception as e:
            print(f'ERROR {sym}: {e}')

asyncio.run(close_all_shorts())
