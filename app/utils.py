import logging
import time
import os
import csv
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

from binance.client import Client
from binance.enums import (
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET
)
from binance.exceptions import BinanceAPIException

from app.config import Config

logger = logging.getLogger('main_logger')
error_logger = logging.getLogger('error_logger')


class BinanceHelper:
    def __init__(self):
        # Initialize the Binance futures client (testnet or live)
        if Config.USE_TESTNET:
            self.client = Client(Config.API_KEY, Config.API_SECRET, testnet=True)
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        else:
            self.client = Client(Config.API_KEY, Config.API_SECRET, tld=Config.BINANCE_TLD)

    def get_symbol_info(self, ticker):
        """
        Fetch symbol filters (tickSize, stepSize, pricePrecision, quantityPrecision).
        """
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
        """
        Round 'value' down to the nearest multiple of 'step_size'.
        """
        value = Decimal(value)
        adjusted = (value // step_size) * step_size
        precision = abs(step_size.as_tuple().exponent)
        return adjusted.quantize(Decimal(f'1e-{precision}'), rounding=ROUND_DOWN)

    def format_val(self, value, precision):
        """
        Format a Decimal 'value' to a string with 'precision' decimal places.
        """
        return f"{Decimal(value):.{precision}f}"

    def log_transaction(self, *args):
        """
        Append a transaction line to 'transactions.csv' inside a 'logs/' folder.
        """
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        csv_path = os.path.join(log_dir, "transactions.csv")
        if not os.path.exists(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Tidspunkt", "Type", "Inn", "Inn-Valuta", "Ut", "Ut-Valuta",
                    "Gebyr", "Gebyr-Valuta", "Marked", "Notat"
                ])

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), *args])

    def fetch_order_commission(self, ticker, order_id):
        """
        Retrieve the total commission and commission asset for a given order ID.
        """
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
        """
        Cancel any open TAKE_PROFIT_MARKET, STOP_MARKET, or TRAILING_STOP_MARKET orders for 'ticker'.
        """
        try:
            open_orders = self.client.futures_get_open_orders(symbol=ticker)
            for order in open_orders:
                if order['type'] in [
                    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                    FUTURE_ORDER_TYPE_STOP_MARKET,
                    FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET
                ]:
                    self.client.futures_cancel_order(symbol=ticker, orderId=order['orderId'])
                    logger.info(f"Cancelled order {order['orderId']} of type {order['type']}")
        except BinanceAPIException as e:
            error_logger.error(f"Error cancelling related orders: {str(e)}")

    def monitor_children_and_cancel(self, ticker, child_order_ids, poll_interval=5):
        """
        Poll open orders every 'poll_interval' seconds. As soon as one of the 'child_order_ids'
        is FILLED, cancel the remaining exit orders. If the position goes to zero, also cancel all.
        """
        while True:
            try:
                # 1) If no open position, cancel all exit orders and return
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
                        logger.info(f"Child order {oid} filled. Cancelling remaining exit orders.")
                        self.cancel_related_orders(ticker)
                        return

            except Exception as e:
                error_logger.error(f"Error monitoring child orders for {ticker}: {e}")
                return

            time.sleep(poll_interval)

    def poll_order_status(
        self,
        ticker,
        order_id,
        action,
        quantity,
        adjusted_price,
        leverage,
        take_profit_percent=None,
        stop_loss_percent=None,
        trailing_stop_percentage=None,
        max_wait=300
    ):
        """
        Poll the LIMIT entry order (ID=order_id) every 15 seconds until one of:
          - status == FILLED   → log commission, return
          - status in [CANCELED, REJECTED, EXPIRED] → log and return
          - timeout (elapsed >= max_wait) → cancel LIMIT, send fallback MARKET to avoid missing trade
        """
        elapsed = 0
        interval = 15

        while elapsed < max_wait:
            try:
                status = self.client.futures_get_order(symbol=ticker, orderId=order_id)['status']
                if status == 'FILLED':
                    time.sleep(5)  # give Binance a moment to finalize trades
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

        # Timeout reached → send fallback MARKET
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
        """
        Places a LIMIT entry based on payload, waits for it to fill, then places
        ROI-based TP/SL/Trailing‐Stop orders and monitors them. If an opposite-side
        position already exists, flatten it first.
        """
        ticker      = payload['ticker']
        new_action  = payload['strategy']['order_action'].upper()   # "BUY" or "SELL"
        order_price = Decimal(str(payload['bar']['order_price']))
        leverage    = Decimal(str(payload['leverage']))
        percent_eq  = Decimal(str(payload['percent_of_equity'])) / Decimal('100')

        # === Read ROI-based exit params from payload ===
        tp_roi_pct    = Decimal(str(payload.get('take_profit_percent', 0)))
        sl_roi_pct    = Decimal(str(payload.get('stop_loss_percent', 0)))
        trail_roi_pct = Decimal(str(payload.get('trailing_stop_percentage', 0)))

        # === 1) Safely check current position ===
        positions = self.client.futures_position_information(symbol=ticker)
        if not positions:
            current_amt = Decimal('0')
        else:
            pos_entry = positions[0]
            current_amt = Decimal(pos_entry.get('positionAmt', '0'))

        # === 2) If opposite position exists, exit it first ===
        if current_amt > 0 and new_action == "SELL":
            # We’re long but need to go short → flatten the long
            self.handle_exit_trade({
                'ticker': ticker,
                'strategy': {'order_action': 'SELL'},
                'bar': {'order_price': order_price}
            })
            # Wait until Binance reports positionAmt == 0
            while True:
                positions = self.client.futures_position_information(symbol=ticker)
                if not positions or Decimal(positions[0].get('positionAmt', '0')) == 0:
                    break
                time.sleep(1)

        elif current_amt < 0 and new_action == "BUY":
            # We’re short but need to go long → flatten the short
            self.handle_exit_trade({
                'ticker': ticker,
                'strategy': {'order_action': 'BUY'},
                'bar': {'order_price': order_price}
            })
            while True:
                positions = self.client.futures_position_information(symbol=ticker)
                if not positions or Decimal(positions[0].get('positionAmt', '0')) == 0:
                    break
                time.sleep(1)

        # === 3) Now no opposite position remains; proceed to open new ===
        symbol_info    = self.get_symbol_info(ticker)
        adjusted_price = self.adjust_to_step(order_price, symbol_info['tick_size'])
        self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))

        margin = Decimal(self.client.futures_account().get('availableBalance', '0'))
        qty    = (margin * leverage * percent_eq) / adjusted_price
        adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

        notional = adjusted_qty * adjusted_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Trade value too low")

        # === 4) Place the new LIMIT entry order ===
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

        # === 5) Wait for LIMIT to fill (or fallback to MARKET) ===
        self.poll_order_status(ticker, entry_id, new_action, adjusted_qty, adjusted_price, leverage)

        # === 6) Entry is now filled at 'adjusted_price' ===
        entry_price = adjusted_price

        # === 7) Place ROI-based TP / SL / Trailing-Stop orders ===
        tp_id, sl_id, trail_id = None, None, None

        # — Take-Profit (ROI → price) —
        if tp_roi_pct > 0:
            if new_action == "BUY":
                factor_tp = Decimal('1') + (tp_roi_pct / Decimal('100') / leverage)
            else:  # "SELL"
                factor_tp = Decimal('1') - (tp_roi_pct / Decimal('100') / leverage)
            take_profit_price = entry_price * factor_tp
            tp_price_adj = self.adjust_to_step(take_profit_price, symbol_info['tick_size'])

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

        # — Stop-Loss (ROI → price) —
        if sl_roi_pct > 0:
            if new_action == "BUY":
                factor_sl = Decimal('1') - (sl_roi_pct / Decimal('100') / leverage)
            else:  # "SELL"
                factor_sl = Decimal('1') + (sl_roi_pct / Decimal('100') / leverage)
            stop_loss_price = entry_price * factor_sl
            sl_price_adj = self.adjust_to_step(stop_loss_price, symbol_info['tick_size'])

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

        # — Trailing-Stop (ROI → callbackRate, clamped to ≥0.1%) —
        if trail_roi_pct > 0:
            raw_callback = float(trail_roi_pct / leverage)     # e.g. 0.5 ÷ 75 ≈ 0.0067
            # Binance minimum callbackRate is 0.1%
            callback_rate_pct = max(raw_callback, 0.1)

            resp_trail = self.client.futures_create_order(
                symbol=ticker,
                side=("SELL" if new_action == "BUY" else "BUY"),
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                callbackRate=callback_rate_pct,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                reduceOnly=True
            )
            trail_id = resp_trail['orderId']
            if raw_callback < 0.1:
                logger.info(
                    f"Placed TRAILING_STOP_MARKET (ID={trail_id}) with clamped callbackRate={callback_rate_pct}% "
                    f"(raw {raw_callback:.6f}% was below minimum)"
                )
            else:
                logger.info(f"Placed TRAILING_STOP_MARKET (ID={trail_id}) callbackRate={callback_rate_pct}%")

        # === 8) Monitor child exit orders to cancel siblings ===
        child_ids = [oid for oid in (tp_id, sl_id, trail_id) if oid is not None]
        if child_ids:
            self.monitor_children_and_cancel(ticker, child_ids)

        # === 9) Log the ENTRY transaction to CSV ===
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


    def handle_exit_trade(self, payload):
        """
        Cancels any existing TP/SL/TS orders, then sends a MARKET reduce-only
        to close the current position fully. Logs the exit commission and notional.
        """
        ticker      = payload['ticker']
        action      = payload['strategy']['order_action'].upper()  # "BUY" or "SELL"
        order_price = Decimal(str(payload['bar']['order_price']))

        # 1) Get current position size (absolute value)
        positions = self.client.futures_position_information(symbol=ticker)
        if not positions:
            return  # nothing to exit
        position = positions[0]
        amount = abs(Decimal(position.get('positionAmt', '0')))

        symbol_info = self.get_symbol_info(ticker)
        adjusted_qty = self.adjust_to_step(amount, symbol_info['step_size'])

        # 2) Minimum-notional check
        notional = adjusted_qty * order_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Exit trade value too low")

        # 3) Cancel any outstanding TP/SL/Trailing orders
        self.cancel_related_orders(ticker)

        # 4) Send MARKET order to close the full position
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

        # 6) Log the EXIT transaction
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
