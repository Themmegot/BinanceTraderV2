import logging
import os
import csv
import time
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
        """
        Writes a single row to 'transactions.csv' that matches kryptosekken’s
        “Generisk CSV” format:

        Columns (exactly in this order):
          Dato,Kvantitet mottatt,Valuta mottatt,
          Kvantitet sendt,Valuta sendt,
          Gebyr beløp,Gebyr valuta,
          Transaksjonstype,Notat

        We infer the mapping from the existing arguments:
          args[0] = "ENTER" or "EXIT"  → Transaksjonstype
          args[1] = notional (string)  → USDT amount sent/received
          args[2] = the “sent asset”  → e.g. "USDT"
          args[3] = quantity (string)  → BTC amount sent/received
          args[4] = the “received asset” → e.g. "BTC"
          args[5] = commission (string) → fee amount
          args[6] = commission asset    → fee currency ("USDT", etc.)
          args[7] = market/pair         → e.g. "BTCUSDT" (ignored in final CSV)
          args[8] = note (string)       → e.g. "Order 4474211572"

        From that:
          - If Transaksjonstype == "ENTER", then we “send USDT” and “receive BTC.”
          - If Transaksjonstype == "EXIT",  then we “send BTC” and “receive USDT.”
        We format date as "DD.MM.YYYY HH:MM:SS" and write one combined row.
        """
        # Kryptosekken’s exact header (must be in this order):
        header = [
            "Dato",
            "Kvantitet mottatt",
            "Valuta mottatt",
            "Kvantitet sendt",
            "Valuta sendt",
            "Gebyr beløp",
            "Gebyr valuta",
            "Transaksjonstype",
            "Notat"
        ]

        csv_dir = "logs"
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, "transactions.csv")

        # If file does not exist yet, write header row first
        if not os.path.exists(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)

        # Unpack the incoming arguments
        # (As described above in the docstring.)
        (
            tx_type,           # "ENTER" or "EXIT"
            notional_str,      # e.g. "40460.68000"
            sent_asset,        # e.g. "USDT"
            qty_str,           # e.g. "0.391"
            received_asset,    # e.g. "BTC"
            fee_str,           # e.g. "8.09213600"
            fee_asset,         # e.g. "USDT"
            market_pair,       # e.g. "BTCUSDT" (not used in final CSV)
            note               # e.g. "Order 4474211572"
        ) = args

        # Determine “received” vs “sent” amounts for kryptosekken’s columns
        # If tx_type == "ENTER", we “sent” USDT (notional_str) and “received” BTC (qty_str).
        # If tx_type == "EXIT", we “sent” BTC (qty_str) and “received” USDT (notional_str).

        if tx_type.upper() == "ENTER":
            qty_received      = qty_str
            asset_received    = received_asset        # e.g. "BTC"
            qty_sent          = notional_str
            asset_sent        = sent_asset            # e.g. "USDT"
        else:  # "EXIT"
            qty_received      = notional_str
            asset_received    = sent_asset            # e.g. "USDT"
            qty_sent          = qty_str
            asset_sent        = received_asset        # e.g. "BTC"

        # Format the timestamp (“Dato”) as "DD.MM.YYYY HH:MM:SS"
        now_utc = datetime.utcnow()
        date_str = now_utc.strftime("%d.%m.%Y %H:%M:%S")

        # Prepare the row in the exact column order required
        row = [
            date_str,             # Dato
            qty_received,         # Kvantitet mottatt
            asset_received,       # Valuta mottatt
            qty_sent,             # Kvantitet sendt
            asset_sent,           # Valuta sendt
            fee_str,              # Gebyr beløp
            fee_asset,            # Gebyr valuta
            tx_type.lower(),      # Transaksjonstype  (e.g. "enter" or "exit" or "trade")
            note                  # Notat
        ]

        # Append this row
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

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
        Poll child exit orders and current position every `poll_interval` seconds.
        - As soon as one child order (TP, SL, or TS) shows status 'FILLED', log its fill price
          and commission, cancel all remaining siblings, and return.
        - If the positionAmt drops to zero (e.g. due to a flip), check which child (if any)
          is FILLED, log its details, cancel remaining siblings, and return.
        """
        while True:
            try:
                # 1) Check position size
                positions = self.client.futures_position_information(symbol=ticker)
                position_amt = Decimal('0')
                if positions:
                    position_amt = Decimal(positions[0].get('positionAmt', '0'))

                # If position is flat (0), that means an exit happened—
                # we need to identify which child filled (if any) and log it.
                if position_amt == Decimal('0'):
                    # Fetch each child exit order's status to see which one is FILLED.
                    for oid in child_order_ids:
                        try:
                            order_info = self.client.futures_get_order(symbol=ticker, orderId=oid)
                        except Exception:
                            continue
                        if order_info.get('status') == 'FILLED':
                            # We found the filled exit leg. Log its fill price and commission:
                            avg_price = order_info.get('avgPrice') or order_info.get('price')
                            filled_qty = order_info.get('executedQty', '0')
                            commission, asset = self.fetch_order_commission(ticker, oid)
                            logger.info(
                                f"Exit order {oid} FILLED at avgPrice={avg_price}, "
                                f"quantity={filled_qty}, commission={commission} {asset}"
                            )
                            break

                    # Now cancel any remaining sibling exit orders (if still open)
                    self.cancel_related_orders(ticker)
                    return

                # 2) Position is still open—check each child to see if any is already FILLED
                for oid in child_order_ids:
                    try:
                        order_info = self.client.futures_get_order(symbol=ticker, orderId=oid)
                    except Exception:
                        continue

                    if order_info.get('status') == 'FILLED':
                        # We found a child exit that just filled. Log details:
                        avg_price = order_info.get('avgPrice') or order_info.get('price')
                        filled_qty = order_info.get('executedQty', '0')
                        commission, asset = self.fetch_order_commission(ticker, oid)
                        logger.info(
                            f"Exit order {oid} FILLED at avgPrice={avg_price}, "
                            f"quantity={filled_qty}, commission={commission} {asset}"
                        )

                        # Cancel siblings and return
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
                quantity=self.format_val(quantity, self.get_symbol_info(ticker)['quantity_precision'])
            )
            logger.info(f"Fallback MARKET order for {ticker} placed.")
        except Exception as e:
            error_logger.error(f"Fallback MARKET order failed: {e}")

    def handle_enter_trade(self, payload):
        """
        Places a LIMIT entry based on payload, waits for it to fill, then places
        ROI-based TP/SL/Trailing‐Stop orders and monitors them. If an opposite-side
        position already exists, flatten it first. Prevents pyramiding by skipping
        same-direction signals when already in that direction.
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

        # === 2) Prevent pyramiding: if already in same direction, skip ===
        if current_amt > 0 and new_action == "BUY":
            logger.info(f"Already long {current_amt}; skipping additional BUY signal.")
            return
        if current_amt < 0 and new_action == "SELL":
            logger.info(f"Already short {current_amt}; skipping additional SELL signal.")
            return

        # === 3) If opposite position exists, exit it first ===
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

        # === 4) Now no opposite position remains; proceed to open new ===
        symbol_info    = self.get_symbol_info(ticker)
        adjusted_price = self.adjust_to_step(order_price, symbol_info['tick_size'])
        self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))

        margin = Decimal(self.client.futures_account().get('availableBalance', '0'))
        qty    = (margin * leverage * percent_eq) / adjusted_price
        adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

        notional = adjusted_qty * adjusted_price
        if notional < Decimal(str(Config.MIN_NOTIONAL)):
            raise ValueError("Trade value too low")

        # === 5) Place the new LIMIT entry order ===
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

        # === 6) Wait for LIMIT to fill (or fallback to MARKET) ===
        self.poll_order_status(ticker, entry_id, new_action, adjusted_qty, adjusted_price, leverage)

        # === 7) Entry is now filled at 'adjusted_price' ===
        entry_price = adjusted_price

        # === 8) Place ROI-based TP / SL / Trailing-Stop orders ===
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
            raw_callback = float(trail_roi_pct / leverage)     # e.g. 2% ROI at 20× → 0.1%
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

        # === 9) Monitor child exit orders to cancel siblings ===
        child_ids = [oid for oid in (tp_id, sl_id, trail_id) if oid is not None]
        if child_ids:
            self.monitor_children_and_cancel(ticker, child_ids)

        # === 10) Log the ENTRY transaction to CSV ===
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
