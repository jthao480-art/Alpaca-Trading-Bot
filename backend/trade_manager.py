from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest

from . import config


class TradeManager:
    def __init__(self) -> None:
        self.client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY or config.ALPACA_API_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )

    def get_account(self):
        return self.client.get_account()

    def get_open_positions(self):
        return self.client.get_all_positions()

    def get_open_orders(self):
        return self.client.get_orders()

    def submit_buy_market(self, symbol: str, qty: float):
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        buy_order = self.client.submit_order(order_data=order)

        try:
            filled_avg_price = float(getattr(buy_order, "filled_avg_price", 0) or 0)
            filled_qty = float(getattr(buy_order, "filled_qty", qty) or qty)

            if filled_avg_price > 0 and filled_qty > 0:
                stop_price = round(filled_avg_price * 0.94, 2)
                stop_order = self.client.submit_order(
                    order_data=StopOrderRequest(
                        symbol=symbol,
                        qty=filled_qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        stop_price=stop_price,
                    )
                )
                _ = stop_order
        except Exception:
            pass

        return buy_order

    def submit_sell_market(self, symbol: str, qty: float):
        position_qty = 0.0
        for position in self.get_open_positions():
            if getattr(position, "symbol", None) == symbol:
                position_qty = float(getattr(position, "qty", 0) or 0)
                break

        reserved_qty = 0.0
        for order in self.get_open_orders():
            if getattr(order, "symbol", None) != symbol:
                continue
            if str(getattr(order, "side", "")).lower() != "sell":
                continue
            status = str(getattr(order, "status", "")).lower()
            if status not in {"new", "accepted", "pending_new", "partially_filled"}:
                continue
            reserved_qty += float(getattr(order, "qty", 0) or 0)

        available_qty = max(0.0, position_qty - reserved_qty)
        qty = min(float(qty), available_qty)

        if qty <= 0:
            raise ValueError(f"No available shares to sell for {symbol}")

        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.client.submit_order(order_data=order)

    def close_position(self, symbol: str):
        return self.client.close_position(symbol)

    def close_all_positions(self):
        return self.client.close_all_positions(cancel_orders=True)