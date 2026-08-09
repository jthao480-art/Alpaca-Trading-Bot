import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from backend import config

client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)

f = Path('data/trade_ledger.json')
ledger = json.loads(f.read_text())

updated = 0
since = datetime.now(timezone.utc) - timedelta(days=30)

for symbol, entries in ledger.items():
    for entry in entries:
        if entry.get('status') != 'closed':
            continue
        if entry.get('exit_price'):
            continue  # already has price

        try:
            orders = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                after=since,
                limit=10,
            ))
            for order in orders:
                side = str(getattr(order, 'side', '')).lower()
                status = str(getattr(order, 'status', '')).lower()
                order_type = str(getattr(order, 'type', '')).lower()
                if side == 'sell' and status == 'filled':
                    price = float(getattr(order, 'filled_avg_price', 0) or 0)
                    if price > 0:
                        entry['exit_price'] = price
                        if 'trailing' in order_type:
                            entry['close_reason'] = 'trailing_stop'
                        elif 'stop' in order_type:
                            entry['close_reason'] = 'stop_loss'
                        elif 'limit' in order_type:
                            entry['close_reason'] = 'take_profit'
                        updated += 1
                        print(f'Updated {symbol}: exit={price:.2f} reason={entry["close_reason"]}')
                        break
        except Exception as e:
            print(f'Error {symbol}: {e}')

f.write_text(json.dumps(ledger, indent=2, default=str))
print(f'Done - updated {updated} entries')
