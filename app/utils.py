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
        new_action = payload['strategy']['order_action'].upper()   # "BUY" or "SELL"
        order_price = Decimal(str(payload['bar']['order_price']))
        leverage    = Decimal(str(payload['leverage']))
        percent_eq  = Decimal(str(payload['percent_of_equity'])) / Decimal('100')

        # 1) Read ROI‐based exit params from payload (if you’re using ROI logic)
        tp_roi_pct    = Decimal(str(payload.get('take_profit_percent', 0)))
        sl_roi_pct    = Decimal(str(payload.get('stop_loss_percent', 0)))
        trail_roi_pct = Decimal(str(payload.get('trailing_stop_percentage', 0)))

        # 2) Check current position on this symbol
        pos_info = self.client.futures_position_information(symbol=ticker)[0]
        current_amt = Decimal(pos_info['positionAmt'])    # e.g. "+0.5" means long 0.5; "-0.2" means short 0.2

        # 2a) If we have a long (positionAmt > 0) and new_action is "SELL", or
        #     if we have a short (positionAmt < 0) and new_action is "BUY",
        #     then we need to flatten/exit first
        if current_amt > 0 and new_action == "SELL":
            # We’re long but want to go short—so fully exit the long first:
            self.handle_exit_trade({
                'ticker': ticker,
                'strategy': {'order_action': 'SELL'},   # SELL will close the long
                'bar': {'order_price': order_price}
            })
            # Now wait until positionAmt becomes zero before placing new short:
            while True:
                pos_info = self.client.futures_position_information(symbol=ticker)[0]
                if Decimal(pos_info['positionAmt']) == 0:
                    break
                time.sleep(1)  # poll every 1 second

        elif current_amt < 0 and new_action == "BUY":
            # We’re short but want to go long—exit short first:
            self.handle_exit_trade({
                'ticker': ticker,
                'strategy': {'order_action': 'BUY'},    # BUY will close the short
                'bar': {'order_price': order_price}
            })
            while True:
                pos_info = self.client.futures_position_information(symbol=ticker)[0]
                if Decimal(pos_info['positionAmt']) == 0:
                    break
                time.sleep(1)

        # 3) At this point, we know there is no opposite position open.
        #    Now proceed to open the new position just as before:

        symbol_info   = self.get_symbol_info(ticker)
        adjusted_price= self.adjust_to_step(order_price, symbol_info['tick_size'])
        self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))

        margin = Decimal(self.client.futures_account()['availableBalance'])
        qty    = (margin * leverage * percent_eq) / adjusted_price
        adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

        notional = adjusted_qty * adjusted_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Trade value too low")

        # 4) Place your new limit entry
        entry_order = self.client.futures_create_order(
            symbol=ticker,
            side=new_action,
            type=FUTURE_ORDER_TYPE_LIMIT,
            quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
            price=self.format_val(adjusted_price, symbol_info['price_precision']),
            timeInForce='GTC'
        )
        entry_id = entry_order['orderId']
        logger.info(f"Entering {new_action} LIMIT for {ticker}: ID={entry_id}, price={adjusted_price}, qty={adjusted_qty}")

        # 5) Wait for fill (or fallback to market)
        self.poll_order_status(ticker, entry_id, new_action, adjusted_qty, adjusted_price, leverage)

        # 6) Now place ROI‐based TP/SL/TS orders (as in section 3.1)
        entry_price = adjusted_price
        tp_id, sl_id, trail_id = None, None, None

        # — Take‐Profit (ROI→price) —
        if tp_roi_pct > 0:
            if new_action == "BUY":
                factor_tp = (Decimal('1') + (tp_roi_pct / Decimal('100') / leverage))
            else:  # "SELL"
                factor_tp = (Decimal('1') - (tp_roi_pct / Decimal('100') / leverage))
            tp_price = entry_price * factor_tp
            tp_price_adj = self.adjust_to_step(tp_price, symbol_info['tick_size'])
            resp_tp = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if new_action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=self.format_val(tp_price_adj, symbol_info['price_precision']),
                closePosition=False,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True,
                timeInForce='GTC'
            )
            tp_id = resp_tp['orderId']
            logger.info(f"Placed TP_MARKET (ID={tp_id}) @ {tp_price_adj}")

        # — Stop‐Loss (ROI→price) —
        if sl_roi_pct > 0:
            if new_action == "BUY":
                factor_sl = (Decimal('1') - (sl_roi_pct / Decimal('100') / leverage))
            else:  # "SELL"
                factor_sl = (Decimal('1') + (sl_roi_pct / Decimal('100') / leverage))
            sl_price = entry_price * factor_sl
            sl_price_adj = self.adjust_to_step(sl_price, symbol_info['tick_size'])
            resp_sl = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if new_action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=self.format_val(sl_price_adj, symbol_info['price_precision']),
                closePosition=False,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True,
                timeInForce='GTC'
            )
            sl_id = resp_sl['orderId']
            logger.info(f"Placed SL_MARKET (ID={sl_id}) @ {sl_price_adj}")

        # — Trailing‐Stop (ROI→callbackRate) —
        if trail_roi_pct > 0:
            # callbackRate = ROI% ÷ Leverage
            cb_rate = float(trail_roi_pct / leverage)
            resp_trail = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if new_action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                callbackRate=cb_rate,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True
            )
            trail_id = resp_trail['orderId']
            logger.info(f"Placed TRAILING_STOP_MARKET (ID={trail_id}) callbackRate={cb_rate}%")

        # 7) Monitor child exit orders to cancel siblings
        child_ids = [x for x in (tp_id, sl_id, trail_id) if x is not None]
        if child_ids:
            self.monitor_children_and_cancel(ticker, child_ids)

        # 8) Log the entry transaction
        commission, asset = self.fetch_order_commission(ticker, entry_id)
        self.log_transaction(
            "ENTER",
            str(notional),
            "USDT",
            str(adjusted_qty),
            ticker.replace("USDT", ""),
            str(commission),
            asset,
            ticker,
            f"Order {entry_id}"
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
