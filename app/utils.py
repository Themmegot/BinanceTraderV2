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
        action = payload['strategy']['order_action'].upper()   # "BUY" or "SELL"
        order_price = Decimal(str(payload['bar']['order_price']))
        leverage = Decimal(str(payload['leverage']))
        percent_equity = Decimal(str(payload['percent_of_equity'])) / Decimal('100')

        # === Read ROI‐based exit params ===
        #  e.g. if payload["take_profit_percent"] == 10, that means "10% ROI"
        tp_roi_pct = Decimal(str(payload.get('take_profit_percent', 0)))
        sl_roi_pct = Decimal(str(payload.get('stop_loss_percent', 0)))
        trail_roi_pct = Decimal(str(payload.get('trailing_stop_percentage', 0)))

        symbol_info = self.get_symbol_info(ticker)
        adjusted_price = self.adjust_to_step(order_price, symbol_info['tick_size'])

        # ... (set leverage, calculate qty, place LIMIT entry as before) ...
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
        entry_order_id = order['orderId']
        logger.info(f"Enter trade LIMIT submitted: ID={entry_order_id} @ {adjusted_price}, qty={adjusted_qty}")

        # Wait for it to fill (or timeout). Once filled, entry_price == adjusted_price.
        self.poll_order_status(ticker, entry_order_id, action, adjusted_qty, adjusted_price, leverage)

        # === At this point, entry is filled at 'adjusted_price' ===
        entry_price = adjusted_price

        # === Calculate price‐targets based on ROI ÷ Leverage ===

        # 1) TAKE‐PROFIT‐PRICE (ROI‐based)
        tp_order_id = None
        if tp_roi_pct > 0:
            # For a LONG ("BUY"), price must go up => +; for a SHORT, price must go down => −
            if action == "BUY":
                # Price change % needed = tp_roi_pct / leverage
                price_change_factor = (Decimal('1') + (tp_roi_pct / Decimal('100') / leverage))
            else:  # "SELL" (short)
                price_change_factor = (Decimal('1') - (tp_roi_pct / Decimal('100') / leverage))

            take_profit_price = entry_price * price_change_factor
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
            logger.info(f"Placed TP_MARKET (ID={tp_order_id}) @ {tp_price_adj}")

        # 2) STOP‐LOSS‐PRICE (ROI‐based)
        sl_order_id = None
        if sl_roi_pct > 0:
            if action == "BUY":
                price_change_factor = (Decimal('1') - (sl_roi_pct / Decimal('100') / leverage))
            else:  # "SELL"
                price_change_factor = (Decimal('1') + (sl_roi_pct / Decimal('100') / leverage))

            stop_loss_price = entry_price * price_change_factor
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
            logger.info(f"Placed SL_MARKET (ID={sl_order_id}) @ {sl_price_adj}")

        # 3) TRAILING‐STOP (ROI => price‐percent callbackRate)
        trail_order_id = None
        if trail_roi_pct > 0:
            # The callbackRate for a trailing stop is a price‐percent, not ROI%.
            # If user said "trailing_stop_percentage": 2, interpret that as "2% ROI"?
            #    => price‐percent = 2/Leverage = 0.2% for a 10× position
            callback_rate_pct = float(trail_roi_pct / leverage)  # e.g. 2 ROI ÷ 10x = 0.2%
            resp_trail = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                callbackRate=callback_rate_pct,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True
            )
            trail_order_id = resp_trail['orderId']
            logger.info(f"Placed TRAILING_STOP_MARKET (ID={trail_order_id}) with callbackRate={callback_rate_pct}%")

        # 4) Monitor those child orders so that when one fills, we cancel the rest:
        child_ids = [i for i in (tp_order_id, sl_order_id, trail_order_id) if i is not None]
        if child_ids:
            self.monitor_children_and_cancel(ticker, child_ids)

        # 5) Log the entry to your CSV
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

        # 1) Get the current position size
        position = self.client.futures_position_information(symbol=ticker)[0]
        amount = abs(Decimal(position['positionAmt']))

        symbol_info = self.get_symbol_info(ticker)
        adjusted_qty = self.adjust_to_step(amount, symbol_info['step_size'])

        # 2) Minimum‐notional check
        notional = adjusted_qty * order_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Exit trade value too low")

        # 3) Cancel any outstanding TP/SL/Trailing orders
        self.cancel_related_orders(ticker)

        # 4) Send MARKET order to close the position
        exit_order = self.client.futures_create_order(
            symbol=ticker,
            side=action,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
            reduceOnly=True
        )
        logger.info(f"Exit trade executed for {ticker} with qty {adjusted_qty}")

        # 5) Fetch commission for this exit order
        commission, asset = self.fetch_order_commission(ticker, exit_order['orderId'])

        # 6) Log the transaction
        self.log_transaction(
            "EXIT",
            str(notional),
            "USDT",
            str(adjusted_qty),
            ticker.replace("USDT", ""),
            str(commission),
            asset,
            ticker,
            f"Order {exit_order['orderId']}"
        )
