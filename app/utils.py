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
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)  # ensure logs directory exists

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
        action = payload['strategy']['order_action'].upper()         # "BUY" or "SELL"
        order_price = Decimal(str(payload['bar']['order_price']))    # e.g. "50000"
        leverage = Decimal(str(payload['leverage']))                  # e.g. "10"
        percent_equity = Decimal(str(payload['percent_of_equity'])) / Decimal('100')  # e.g. 0.1 for 10%

        # === NEW: Read the JSON exit params ===
        tp_pct = Decimal(str(payload.get('take_profit_percent', 0)))    # e.g. 10
        sl_pct = Decimal(str(payload.get('stop_loss_percent', 0)))      # e.g. 3
        trail_pct = Decimal(str(payload.get('trailing_stop_percentage', 0)))  # e.g. 2

        symbol_info = self.get_symbol_info(ticker)
        adjusted_price = self.adjust_to_step(order_price, symbol_info['tick_size'])

        # … (unchanged: set leverage, compute qty, place MAIN entry order)
        self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))
        margin = Decimal(self.client.futures_account()['availableBalance'])
        qty = (margin * leverage * percent_equity) / adjusted_price
        adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

        notional = adjusted_qty * adjusted_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Trade value too low")

        # 1) Place the LIMIT entry into the orderbook
        order = self.client.futures_create_order(
            symbol=ticker,
            side=action,
            type=FUTURE_ORDER_TYPE_LIMIT,
            quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
            price=self.format_val(adjusted_price, symbol_info['price_precision']),
            timeInForce='GTC'
        )
        entry_order_id = order['orderId']
        logger.info(f"Enter trade order submitted: {entry_order_id} for {ticker} at {adjusted_price} qty {adjusted_qty}")

        # 2) Wait until the LIMIT entry is FILLED
        self.poll_order_status(
            ticker,
            entry_order_id,
            action,
            adjusted_qty,
            adjusted_price,
            leverage
        )
        # At this point, the LIMIT was FILLED; 'adjusted_price' is our actual entry price.

        # 3) Immediately place TP / SL / Trailing‐Stop orders
        #
        #    Calculate the absolute TP price, SL price, and trailing stop callback.
        #    If action=="BUY", then:
        #       TP price = entry_price * (1 + tp_pct/100)
        #       SL price = entry_price * (1 - sl_pct/100)
        #    If action=="SELL" (short), then:
        #       TP price = entry_price * (1 - tp_pct/100)
        #       SL price = entry_price * (1 + sl_pct/100)
        entry_price = adjusted_price

        # *** TAKE‐PROFIT ***
        tp_order_id = None
        if tp_pct > 0:
            if action == "BUY":
                take_profit_price = entry_price * (Decimal('1') + tp_pct / Decimal('100'))
            else:  # SELL
                take_profit_price = entry_price * (Decimal('1') - tp_pct / Decimal('100'))

            tp_price_adj = self.adjust_to_step(take_profit_price, symbol_info['tick_size'])
            resp_tp = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=self.format_val(tp_price_adj, symbol_info['price_precision']),
                closePosition=False,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True,
                timeInForce='GTC'
            )
            tp_order_id = resp_tp['orderId']
            logger.info(f"Placed TAKE_PROFIT_MARKET (ID={tp_order_id}) @ {tp_price_adj}")

        # *** STOP‐LOSS ***
        sl_order_id = None
        if sl_pct > 0:
            if action == "BUY":
                stop_loss_price = entry_price * (Decimal('1') - sl_pct / Decimal('100'))
            else:  # SELL
                stop_loss_price = entry_price * (Decimal('1') + sl_pct / Decimal('100'))

            sl_price_adj = self.adjust_to_step(stop_loss_price, symbol_info['tick_size'])
            resp_sl = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=self.format_val(sl_price_adj, symbol_info['price_precision']),
                closePosition=False,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True,
                timeInForce='GTC'
            )
            sl_order_id = resp_sl['orderId']
            logger.info(f"Placed STOP_MARKET (ID={sl_order_id}) @ {sl_price_adj}")

        # *** TRAILING‐STOP ***
        trail_order_id = None
        if trail_pct > 0:
            # Binance’s API expects "callbackRate" for trailing stops.
            # e.g. 2 means 2%, so pass Decimal('2')
            resp_trail = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                callbackRate=float(trail_pct),  # API expects a float between 0.1 and 100
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True
            )
            trail_order_id = resp_trail['orderId']
            logger.info(f"Placed TRAILING_STOP_MARKET (ID={trail_order_id}) with callbackRate {trail_pct}%")

        # 4) (Optional) Store these child‐order IDs so that when one fills you can cancel the others
        child_ids = [i for i in (tp_order_id, sl_order_id, trail_order_id) if i is not None]
        # For example, you could pass them into a new method:
        self.monitor_children_and_cancel(ticker, child_ids)

        # 5) Log the transaction (ENTRY) as before
        commission, asset = self.fetch_order_commission(ticker, entry_order_id)
        self.log_transaction(
            "ENTER",
            str(notional),
            "USDT",
            str(adjusted_qty),
            ticker.replace("USDT", ""),
            str(commission),
            asset,
            ticker,
            f"Order {entry_order_id}"
        )

    def monitor_children_and_cancel(self, ticker, child_order_ids, poll_interval=5):
        """
        Periodically check open orders. As soon as one of the child_order_ids is FILLED,
        cancel any of the others. If position goes to zero, also cancel everything.
        """

        while True:
            try:
                # 1) If no open position, cancel all exit‐orders and return
                positions = self.client.futures_position_information(symbol=ticker)
                if not positions or Decimal(positions[0]['positionAmt']) == Decimal('0'):
                    logger.info(f"No open position for {ticker}. Cancelling exit orders.")
                    self.cancel_related_orders(ticker)
                    return

                # 2) Look at open orders right now
                open_orders = self.client.futures_get_open_orders(symbol=ticker)

                for order in open_orders:
                    oid = order['orderId']
                    if oid in child_order_ids and order['status'] == 'FILLED':
                        # One child (TP/SL/TS) got filled.
                        logger.info(f"Child order {oid} filled. Cancelling remaining exit orders.")
                        self.cancel_related_orders(ticker)
                        return

            except Exception as e:
                error_logger.error(f"Error monitoring child orders for {ticker}: {e}")
                return

            time.sleep(poll_interval)


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

        # Writing transaction out to transaction.cvs
        commission, asset = self.fetch_order_commission(ticker, order['orderId'])

        self.log_transaction(
            "EXIT",
            str(notional),
            "USDT",
            str(adjusted_qty),
            ticker.replace("USDT", ""),
            str(commission),
            asset,
            ticker,
            f"Order {order['orderId']}"
        )
