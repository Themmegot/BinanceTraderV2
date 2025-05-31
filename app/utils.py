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
        # Initialize either testnet or live client
        if Config.USE_TESTNET:
            self.client = Client(Config.API_KEY, Config.API_SECRET, testnet=True)
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        else:
            self.client = Client(Config.API_KEY, Config.API_SECRET, tld=Config.BINANCE_TLD)

    # ---------------------------
    #  Symbol‐info helpers
    # ---------------------------

    def get_symbol_info(self, ticker):
        """
        Get the futures symbol info (tickSize, stepSize, precision).
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

    def get_symbol_info_spot(self, ticker):
        """
        Get the spot symbol info (tickSize, stepSize, precision).
        """
        info = self.client.get_symbol_info(ticker)
        if not info:
            raise ValueError(f"Symbol info for {ticker} not found")
        price_filter = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        lot_size_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        return {
            "tick_size": Decimal(price_filter["tickSize"]),
            "step_size": Decimal(lot_size_filter["stepSize"]),
            "price_precision": int(info["quotePrecision"]),
            "quantity_precision": int(info["baseAssetPrecision"])
        }

    # ---------------------------
    #  Rounding / formatting
    # ---------------------------

    def adjust_to_step(self, value, step_size):
        """
        Round 'value' down to the nearest 'step_size' increment.
        """
        value = Decimal(value)
        adjusted = (value // step_size) * step_size
        precision = abs(step_size.as_tuple().exponent)
        return adjusted.quantize(Decimal(f'1e-{precision}'), rounding=ROUND_DOWN)

    def format_val(self, value, precision):
        """
        Format a Decimal or str to a string with exactly 'precision' decimal places.
        """
        return f"{Decimal(value):.{precision}f}"

    # ---------------------------
    #  Commission / fee helpers
    # ---------------------------

    def fetch_order_commission(self, ticker, order_id):
        """
        Given a futures order ID, return (total_commission, commission_asset).
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

    def fetch_spot_commission(self, ticker, order_id):
        """
        Given a spot order ID, return (total_commission, commission_asset).
        """
        try:
            trades = self.client.get_my_trades(symbol=ticker, orderId=order_id)
            total_commission = Decimal("0")
            commission_asset = ""
            for trade in trades:
                commission = Decimal(trade.get("commission", "0"))
                total_commission += commission
                if not commission_asset:
                    commission_asset = trade.get("commissionAsset", "")
            return total_commission, commission_asset
        except Exception as e:
            error_logger.error(f"Error fetching spot commission for order {order_id}: {e}")
            return Decimal("0"), ""

    # ---------------------------
    #  CSV logging for kryptosekken
    # ---------------------------

    def log_transaction(self, *args):
        """
        Write one row to `logs/transactions.csv` in kryptosekken’s Generic CSV format:
          Dato,Kvantitet mottatt,Valuta mottatt,Kvantitet sendt,Valuta sendt,
          Gebyr beløp,Gebyr valuta,Transaksjonstype,Notat

        Args passed in (len=9):
            args[0] = tx_type        ("enter", "exit", "profit", or "loss")
            args[1] = qty_received   (string, e.g. "0.12345678")
            args[2] = asset_received (string, e.g. "BTC" or "USDT")
            args[3] = qty_sent       (string, e.g. "1000.00000000")
            args[4] = asset_sent     (string, e.g. "USDT" or "")
            args[5] = fee_amount     (string, e.g. "2.50000000")
            args[6] = fee_asset      (string, e.g. "USDT")
            args[7] = market         (ignored in CSV, e.g. "BTCUSDT")
            args[8] = note           (string, e.g. "Order 12345")

        Exactly these headers (in this order) will be written if file does not exist:
            [
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
        """
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

        if not os.path.exists(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)

        (
            tx_type,
            qty_received,
            asset_received,
            qty_sent,
            asset_sent,
            fee_amount,
            fee_asset,
            market_pair,  # ignored
            note
        ) = args

        # Format timestamp as "DD.MM.YYYY HH:MM:SS"
        now_utc = datetime.utcnow()
        date_str = now_utc.strftime("%d.%m.%Y %H:%M:%S")

        row = [
            date_str,
            qty_received,
            asset_received,
            qty_sent,
            asset_sent,
            fee_amount,
            fee_asset,
            tx_type,
            note
        ]

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

    # ---------------------------
    #  Cancel open TP/SL/TS orders
    # ---------------------------

    def cancel_related_orders(self, ticker):
        """
        Cancel any open take-profit, stop-loss, or trailing-stop orders for this symbol.
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

    # ---------------------------
    #  Polling child exit orders
    # ---------------------------

    def monitor_children_and_cancel(self, ticker, child_order_ids, poll_interval=5):
        """
        Poll child exit orders (TP / SL / TS) and current position size. As soon as
        one child shows status 'FILLED', log its fill price + commission, cancel siblings,
        then return. If position hits zero unexpectedly (flip), identify which child filled,
        log it, cancel siblings, and return.
        """
        while True:
            try:
                positions = self.client.futures_position_information(symbol=ticker)
                position_amt = Decimal('0')
                if positions:
                    position_amt = Decimal(positions[0].get('positionAmt', '0'))

                if position_amt == Decimal('0'):
                    # Position is flat: figure out which child (if any) filled.
                    for oid in child_order_ids:
                        try:
                            order_info = self.client.futures_get_order(symbol=ticker, orderId=oid)
                        except Exception:
                            continue
                        if order_info.get('status') == 'FILLED':
                            avg_price = order_info.get('avgPrice') or order_info.get('price')
                            filled_qty = order_info.get('executedQty', '0')
                            commission, asset = self.fetch_order_commission(ticker, oid)
                            logger.info(
                                f"Exit order {oid} FILLED at avgPrice={avg_price}, "
                                f"quantity={filled_qty}, commission={commission} {asset}"
                            )
                            break

                    self.cancel_related_orders(ticker)
                    return

                # If position still open, check each child for FILLED
                for oid in child_order_ids:
                    try:
                        order_info = self.client.futures_get_order(symbol=ticker, orderId=oid)
                    except Exception:
                        continue
                    if order_info.get('status') == 'FILLED':
                        avg_price = order_info.get('avgPrice') or order_info.get('price')
                        filled_qty = order_info.get('executedQty', '0')
                        commission, asset = self.fetch_order_commission(ticker, oid)
                        logger.info(
                            f"Exit order {oid} FILLED at avgPrice={avg_price}, "
                            f"quantity={filled_qty}, commission={commission} {asset}"
                        )
                        self.cancel_related_orders(ticker)
                        return

            except Exception as e:
                error_logger.error(f"Error monitoring child orders for {ticker}: {e}")
                return

            time.sleep(poll_interval)

    # ---------------------------
    #  Poll a LIMIT until filled (or timeout)
    # ---------------------------

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
        Poll a futures LIMIT entry order for up to max_wait seconds. If it never fills,
        cancel it and immediately place a fallback MARKET and log that entry.
        """
        elapsed = 0
        interval = 15

        while elapsed < max_wait:
            try:
                status = self.client.futures_get_order(symbol=ticker, orderId=order_id)['status']
                if status == 'FILLED':
                    # Normal LIMIT fill path → log as a futures entry
                    time.sleep(5)
                    commission, asset = self.fetch_order_commission(ticker, order_id)
                    logger.info(f"Order {order_id} filled. Commission: {commission} {asset}")

                    # Compute notional = filled_price × quantity
                    order_info = self.client.futures_get_order(symbol=ticker, orderId=order_id)
                    fill_price = Decimal(order_info.get('avgPrice') or order_info.get('price'))
                    filled_qty = Decimal(order_info.get('executedQty', '0'))
                    notional = fill_price * filled_qty

                    # Log this futures entry as full notional (so you can keep track of position size).
                    self.log_transaction(
                        "enter",
                        f"{filled_qty:.8f}",          # Kvantitet mottatt (BTC)
                        ticker.replace("USDT", ""),  # "BTC"
                        f"{notional:.8f}",           # Kvantitet sendt (USDT)
                        "USDT",
                        f"{commission:.8f}",
                        asset,
                        ticker,
                        f"Order {order_id}"
                    )
                    return

                elif status in ['CANCELED', 'REJECTED', 'EXPIRED']:
                    logger.info(f"Order {order_id} status: {status}. Exiting poll.")
                    return

            except Exception as e:
                error_logger.error(f"Polling error for order {order_id}: {e}")
                return

            time.sleep(interval)
            elapsed += interval

        # TIMEOUT → fallback MARKET
        logger.warning(f"Order {order_id} not filled in time. Cancelling and sending fallback MARKET order.")
        try:
            # 1) Cancel the stale LIMIT
            self.client.futures_cancel_order(symbol=ticker, orderId=order_id)

            # 2) Place MARKET to open the position
            fallback_order = self.client.futures_create_order(
                symbol=ticker,
                side=action,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=self.format_val(quantity, self.get_symbol_info(ticker)['quantity_precision'])
            )
            fallback_id = fallback_order['orderId']
            logger.info(f"Fallback MARKET order for {ticker} placed (ID={fallback_id}).")

            # 3) Wait a moment, fetch fill details
            time.sleep(2)
            filled_info = self.client.futures_get_order(symbol=ticker, orderId=fallback_id)
            filled_price = Decimal(filled_info.get('avgPrice') or filled_info.get('price'))
            filled_qty   = Decimal(filled_info.get('executedQty', '0'))
            commission, fee_asset = self.fetch_order_commission(ticker, fallback_id)

            # Compute notional owned: filled_price × filled_qty
            notional = filled_price * filled_qty

            # 4) Log fallback futures entry
            self.log_transaction(
                "enter",
                f"{filled_qty:.8f}",           # Kvantitet mottatt (BTC)
                ticker.replace("USDT", ""),   # "BTC"
                f"{notional:.8f}",            # Kvantitet sendt (USDT)
                "USDT",
                f"{commission:.8f}",
                fee_asset,
                ticker,
                f"Order {fallback_id} (fallback)"
            )

        except Exception as e:
            error_logger.error(f"Fallback MARKET order failed: {e}")

    # ---------------------------
    #  Place trades
    # ---------------------------

    def handle_enter_trade(self, payload):
        """
        Unified “enter” handler. Detects futures vs spot by checking for “leverage”:
          - If payload contains "leverage", do a futures order.
          - Otherwise, do a spot order.
        """
        is_futures = ("leverage" in payload and payload["leverage"] is not None)
        ticker     = payload["ticker"]
        action     = payload["strategy"]["order_action"].upper()  # "BUY" or "SELL"

        if is_futures:
            # ----------------
            #  FUTURES ENTRY
            # ----------------
            order_price = Decimal(str(payload["bar"]["order_price"]))
            leverage    = Decimal(str(payload["leverage"]))
            percent_equity = Decimal(str(payload["percent_of_equity"])) / Decimal("100")

            # 1) Set leverage
            self.client.futures_change_leverage(symbol=ticker, leverage=int(leverage))

            # 2) Calculate position size based on margin balance
            margin = Decimal(self.client.futures_account()['availableBalance'])
            qty    = (margin * leverage * percent_equity) / order_price
            symbol_info = self.get_symbol_info(ticker)
            adjusted_qty = self.adjust_to_step(qty, symbol_info['step_size'])

            # 3) Check MIN_NOTIONAL
            notional = adjusted_qty * order_price
            if notional < Decimal(str(Config.MIN_NOTIONAL)):
                raise ValueError("Trade value too low")

            # 4) Place LIMIT order
            order = self.client.futures_create_order(
                symbol=ticker,
                side=action,
                type=FUTURE_ORDER_TYPE_LIMIT,
                quantity=self.format_val(adjusted_qty, symbol_info['quantity_precision']),
                price=self.format_val(order_price, symbol_info['price_precision']),
                timeInForce='GTC'
            )
            entry_id = order['orderId']
            logger.info(f"Entering {action} LIMIT for {ticker}: ID={entry_id}, price={order_price}, qty={adjusted_qty}")

            # 5) Poll until filled (or fallback to MARKET)
            self.poll_order_status(
                ticker,
                entry_id,
                action,
                adjusted_qty,
                order_price,
                leverage,
                take_profit_percent=payload.get("take_profit_percent"),
                stop_loss_percent=payload.get("stop_loss_percent"),
                trailing_stop_percentage=payload.get("trailing_stop_percentage")
            )

            # 6) (Optional) Place TP/SL/TS children based on payload—omitted here for brevity.
            #    You can call your existing logic to place those three if payload specifies >0.

        else:
            # ----------------
            #   SPOT ENTRY
            # ----------------
            order_price = Decimal(str(payload["bar"]["order_price"]))
            percent_equity = Decimal(str(payload["percent_of_equity"])) / Decimal("100")
            symbol_info = self.get_symbol_info_spot(ticker)

            # 1a) Fetch USDT balance
            balance = Decimal(self.client.get_asset_balance(asset="USDT")["free"])
            raw_qty = (balance * percent_equity) / order_price
            qty = self.adjust_to_step(raw_qty, symbol_info["step_size"])

            # 1b) Check MIN_NOTIONAL (spot in config?): you can reuse Config.MIN_NOTIONAL
            notional = qty * order_price
            if notional < Decimal(str(Config.MIN_NOTIONAL)):
                raise ValueError("Trade value too low")

            # 2) Place a spot LIMIT; fallback to MARKET if never fills
            spot_order = self.client.create_order(
                symbol=ticker,
                side=action,
                type="LIMIT",
                timeInForce="GTC",
                quantity=self.format_val(qty, symbol_info["quantity_precision"]),
                price=self.format_val(order_price, symbol_info["price_precision"])
            )
            entry_id = spot_order["orderId"]
            logger.info(f"Entering {action} LIMIT (spot) for {ticker}: ID={entry_id}, price={order_price}, qty={qty}")

            # 3) Poll until FILLED (two ways: either poll get_order until status=="FILLED",
            #    or set a short timeout and fallback to market). For brevity, here's a simple
            #    polling loop (max 60s):
            elapsed = 0
            interval = 5
            while elapsed < 60:
                try:
                    info = self.client.get_order(symbol=ticker, orderId=entry_id)
                    status = info["status"]
                    if status == "FILLED":
                        filled_qty = Decimal(info["executedQty"])
                        fill_price = (Decimal(info["cummulativeQuoteQty"]) / filled_qty) if filled_qty != 0 else order_price
                        commission, fee_asset = self.fetch_spot_commission(ticker, entry_id)
                        logger.info(f"Spot order {entry_id} filled at {fill_price}, qty={filled_qty}, fee={commission} {fee_asset}")

                        # Log full‐notional spot entry
                        notional_real = fill_price * filled_qty
                        self.log_transaction(
                            "enter",
                            f"{filled_qty:.8f}",               # BTC received
                            ticker.replace("USDT", ""),
                            f"{notional_real:.8f}",            # USDT spent
                            "USDT",
                            f"{commission:.8f}",
                            fee_asset,
                            ticker,
                            f"Order {entry_id}"
                        )
                        break

                    elif status in ["CANCELED", "REJECTED", "EXPIRED"]:
                        logger.info(f"Spot order {entry_id} status: {status}. Exiting spot‐enter.")
                        return

                except Exception as e:
                    error_logger.error(f"Error polling spot order {entry_id}: {e}")
                    return

                time.sleep(interval)
                elapsed += interval

            else:
                # TIMEOUT → fallback to MARKET
                logger.warning(f"Spot order {entry_id} not filled in time. Falling back to MARKET.")
                try:
                    self.client.cancel_order(symbol=ticker, orderId=entry_id)
                    mkt_order = self.client.create_order(
                        symbol=ticker,
                        side=action,
                        type="MARKET",
                        quantity=self.format_val(qty, symbol_info["quantity_precision"])
                    )
                    mkt_id = mkt_order["orderId"]
                    time.sleep(2)
                    info = self.client.get_order(symbol=ticker, orderId=mkt_id)
                    filled_qty = Decimal(info["executedQty"])
                    fill_price = (Decimal(info["cummulativeQuoteQty"]) / filled_qty) if filled_qty != 0 else order_price
                    commission, fee_asset = self.fetch_spot_commission(ticker, mkt_id)
                    logger.info(f"Spot fallback MARKET {mkt_id} filled at {fill_price}, qty={filled_qty}, fee={commission} {fee_asset}")

                    notional_real = fill_price * filled_qty
                    self.log_transaction(
                        "enter",
                        f"{filled_qty:.8f}",               # BTC received
                        ticker.replace("USDT", ""),
                        f"{notional_real:.8f}",            # USDT spent
                        "USDT",
                        f"{commission:.8f}",
                        fee_asset,
                        ticker,
                        f"Order {mkt_id} (fallback)"
                    )

                except Exception as e:
                    error_logger.error(f"Spot fallback MARKET failed: {e}")

    # ---------------------------
    #  Exit trades
    # ---------------------------

    def handle_exit_trade(self, payload):
        """
        Unified “exit” handler. Detects futures vs spot similarly:
          - If payload contains “leverage”, do a futures exit (compute P&L, log profit/loss).
          - Otherwise, do a spot exit (log full notional).
        """
        is_futures = ("leverage" in payload and payload["leverage"] is not None)
        ticker     = payload["ticker"]
        action     = payload["strategy"]["order_action"].upper()  # “SELL” if closing a long, “BUY” if closing a short

        if is_futures:
            # ----------------
            #  FUTURES EXIT
            # ----------------
            # 1) Get current position to find entryPrice & qty
            pos    = self.client.futures_position_information(symbol=ticker)[0]
            qty    = abs(Decimal(pos["positionAmt"]))
            entry_price = Decimal(pos["entryPrice"])  # e.g. 10000

            # 2) Place MARKET exit
            exit_order = self.client.futures_create_order(
                symbol=ticker,
                side=action,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=self.format_val(qty, self.get_symbol_info(ticker)["quantity_precision"]),
                reduceOnly=True
            )
            exit_id = exit_order["orderId"]

            # 3) Wait & fetch fill details + commission
            time.sleep(2)
            exit_info = self.client.futures_get_order(symbol=ticker, orderId=exit_id)
            exit_price = Decimal(exit_info.get("avgPrice") or exit_info.get("price"))
            filled_qty = Decimal(exit_info.get("executedQty", "0"))
            commission, fee_asset = self.fetch_order_commission(ticker, exit_id)
            logger.info(f"Futures exit {exit_id} filled at {exit_price}, qty={filled_qty}, fee={commission} {fee_asset}")

            # 4) Compute P&L: (exitPrice - entryPrice) * qty for a long; inverse for a short.
            if action == "SELL":
                pnl = (exit_price - entry_price) * qty
            else:  # “BUY” to close a short
                pnl = (entry_price - exit_price) * qty

            # 5) Log only net P&L (profit or loss)
            if pnl >= 0:
                self.log_transaction(
                    "profit",
                    f"{pnl:.8f}",    # USDT gained
                    "USDT",
                    "0.00000000",    # no BTC sent on a profit‐only line
                    "",
                    f"{commission:.8f}",
                    fee_asset,
                    ticker,
                    f"Exit {exit_id}"
                )
            else:
                loss_amt = abs(pnl)
                self.log_transaction(
                    "loss",
                    "0.00000000",
                    "",
                    f"{loss_amt:.8f}",
                    "USDT",
                    f"{commission:.8f}",
                    fee_asset,
                    ticker,
                    f"Exit {exit_id}"
                )

        else:
            # ----------------
            #   SPOT EXIT
            # ----------------
            order_price = Decimal(str(payload["bar"]["order_price"]))
            symbol_info = self.get_symbol_info_spot(ticker)

            # 1a) Determine how much BTC we have to sell
            base_asset = ticker.replace("USDT", "")  # e.g. "BTC"
            balance = Decimal(self.client.get_asset_balance(asset=base_asset)["free"])
            qty = self.adjust_to_step(balance, symbol_info["step_size"])

            # 1b) Place a MARKET sell on spot
            spot_order = self.client.create_order(
                symbol=ticker,
                side=action,  # “SELL”
                type="MARKET",
                quantity=self.format_val(qty, symbol_info["quantity_precision"])
            )
            exit_id = spot_order["orderId"]

            # 1c) Fetch fill details & commission
            time.sleep(2)
            exit_info = self.client.get_order(symbol=ticker, orderId=exit_id)
            filled_qty = Decimal(exit_info["executedQty"])
            # exit_price = totalQuoteQty / filled_qty
            exit_price = (Decimal(exit_info["cummulativeQuoteQty"]) / filled_qty) if filled_qty != 0 else order_price
            commission, fee_asset = self.fetch_spot_commission(ticker, exit_id)
            logger.info(f"Spot exit {exit_id} filled at {exit_price}, qty={filled_qty}, fee={commission} {fee_asset}")

            # 2) Compute notional = exit_price × qty
            notional = exit_price * filled_qty

            # 3) Log full notional spot exit
            self.log_transaction(
                "exit",
                f"{notional:.8f}",         # Kvantitet mottatt (USDT)
                "USDT",
                f"{filled_qty:.8f}",       # Kvantitet sendt (BTC)
                base_asset,
                f"{commission:.8f}",
                fee_asset,
                ticker,
                f"Exit {exit_id}"
            )
