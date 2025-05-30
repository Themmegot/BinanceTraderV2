import logging
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from binance.enums import (
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET
)
from binance.exceptions import BinanceAPIException
from app.config import Config
from datetime import datetime
import csv
import os
import time

logger = logging.getLogger('main_logger')
error_logger = logging.getLogger('error_logger')

class BinanceHelper:
    def __init__(self):
        if Config.USE_TESTNET:
            self.client = Client(Config.API_KEY, Config.API_SECRET, testnet=True)
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        else:
            self.client = Client(Config.API_KEY, Config.API_SECRET, tld=Config.BINANCE_TLD)

    def get_symbol_info(self, ticker):
        exchange_info = self.client.futures_exchange_info()
        symbol_info = next((s for s in exchange_info['symbols'] if s['symbol'] == ticker), None)
        if not symbol_info:
            raise ValueError(f"Symbol info for {ticker} not found")
        price_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER')
        lot_size_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
        return {
            "tick_size": Decimal(price_filter['tickSize']),
            "step_size": Decimal(lot_size_filter['stepSize']),
            "price_precision": int(symbol_info['pricePrecision']),
            "quantity_precision": int(symbol_info['quantityPrecision'])
        }

    def adjust_to_step(self, value, step_size):
        value = Decimal(value)
        adjusted = (value // step_size) * step_size
        precision = abs(step_size.as_tuple().exponent)
        return adjusted.quantize(Decimal(f'1e-{precision}'), rounding=ROUND_DOWN)

    def format_val(self, value, precision):
        return f"{Decimal(value):.{precision}f}"

    def log_transaction(self, *args):
        if not os.path.exists("transactions.csv"):
            with open("transactions.csv", "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Tidspunkt", "Type", "Inn", "Inn-Valuta", "Ut", "Ut-Valuta", "Gebyr", "Gebyr-Valuta", "Marked", "Notat"])
        with open("transactions.csv", "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), *args])

    def fetch_order_commission(self, ticker, order_id):
        try:
            trades = self.client.futures_account_trades(symbol=ticker, orderId=order_id)
            total_commission = Decimal("0")
            commission_asset = ""
            for trade in trades:
                commission = Decimal(trade.get("commission", "0"))
                total_commission += commission
                if not commission_asset:
                    commission_asset = trade.get("commissionAsset", "")
            return total_commission, commission_asset
        except Exception as e:
            error_logger.error(f"Error fetching commission for order {order_id}: {e}")
            return Decimal("0"), ""

    def cancel_related_orders(self, ticker):
        try:
            open_orders = self.client.futures_get_open_orders(symbol=ticker)
            for order in open_orders:
                if order['type'] in [FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                                     FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                                     FUTURE_ORDER_TYPE_STOP_MARKET]:
                    self.client.futures_cancel_order(symbol=ticker, orderId=order['orderId'])
                    logger.info(f"Cancelled order {order['orderId']} of type {order['type']}")
        except BinanceAPIException as e:
            error_logger.error(f"Error cancelling related orders: {str(e)}")

    def monitor_exit_orders(self, ticker, order_ids, poll_interval=10):
        while True:
            try:
                positions = self.client.futures_position_information(symbol=ticker)
                if not positions or Decimal(positions[0]['positionAmt']) == Decimal('0'):
                    logger.info(f"No open position for {ticker}. Cancelling exit orders.")
                    self.cancel_related_orders(ticker)
                    return
                open_orders = self.client.futures_get_open_orders(symbol=ticker)
                for order in open_orders:
                    if order['orderId'] in order_ids and order['status'] == 'FILLED':
                        logger.info(f"Order {order['orderId']} is filled. Cancelling remaining exit orders for {ticker}.")
                        self.cancel_related_orders(ticker)
                        return
            except Exception as e:
                error_logger.error(f"Error monitoring exit orders: {e}")
                return
            time.sleep(poll_interval)

    def poll_order_status(self, ticker, order_id, action, quantity, adjusted_price, leverage,
                          take_profit_percent=None, stop_loss_percent=None, trailing_stop_percentage=None,
                          max_wait=300):
        elapsed = 0
        interval = 15
        while elapsed < max_wait:
            try:
                status = self.client.futures_get_order(symbol=ticker, orderId=order_id)['status']
                if status == 'FILLED':
                    time.sleep(5)
                    commission, asset = self.fetch_order_commission(ticker, order_id)
                    logger.info(f"Order {order_id} filled. Commission: {commission} {asset}")
                    return
                elif status in ['CANCELED', 'REJECTED', 'EXPIRED']:
                    logger.info(f"Order {order_id} status: {status}. Exiting poll.")
                    return
            except Exception as e:
                error_logger.error(f"Polling error for order {order_id}: {e}")
                return
            time.sleep(interval)
            elapsed += interval

        logger.warning(f"Order {order_id} not filled in time. Cancelling and sending fallback MARKET order.")
        try:
            self.client.futures_cancel_order(symbol=ticker, orderId=order_id)
            self.client.futures_create_order(
                symbol=ticker,
                side=action,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=self.format_val(quantity, self.get_symbol_info(ticker)['quantity_precision']),
                reduceOnly=True
            )
            logger.info(f"Fallback MARKET order for {ticker} placed.")
        except Exception as e:
            error_logger.error(f"Fallback MARKET order failed: {e}")

    def handle_enter_trade(self, payload):
        ticker = payload['ticker']
        action = payload['strategy']['order_action'].upper()
        order_price = Decimal(str(payload['bar']['order_price']))
        leverage = Decimal(str(payload['leverage']))
        percent_equity = Decimal(str(payload['percent_of_equity'])) / Decimal('100')

        symbol_info = self.get_symbol_info(ticker)
        adjusted_price = self.adjust_to_step(order_price, symbol_info['tick_size'])

        self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))
        margin = Decimal(self.client.futures_account()['availableBalance'])
        qty = (margin * leverage * percent_equity) / adjusted_price
        adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

        notional = adjusted_qty * adjusted_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Trade value too low")

        order = self.client.futures_create_order(
            symbol=ticker,
            side=action,
            type=FUTURE_ORDER_TYPE_LIMIT,
            quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
            price=self.format_val(adjusted_price, symbol_info['price_precision']),
            timeInForce='GTC'
        )

        logger.info(f"Switch trade order submitted: {order['orderId']} for {ticker} at {adjusted_price} with qty {adjusted_qty}")
        self.poll_order_status(ticker, order['orderId'], action, adjusted_qty, adjusted_price, leverage)

    def handle_exit_trade(self, payload):
        ticker = payload['ticker']
        action = payload['strategy']['order_action'].upper()
        order_price = Decimal(str(payload['bar']['order_price']))

        position = self.client.futures_position_information(symbol=ticker)[0]
        amount = abs(Decimal(position['positionAmt']))

        symbol_info = self.get_symbol_info(ticker)
        adjusted_qty = self.adjust_to_step(amount, symbol_info['step_size'])

        notional = adjusted_qty * order_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Exit trade value too low")

        self.cancel_related_orders(ticker)

        self.client.futures_create_order(
            symbol=ticker,
            side=action,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
            reduceOnly=True
        )

        logger.info(f"Exit trade executed for {ticker} with qty {adjusted_qty}")
