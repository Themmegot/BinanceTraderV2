from app.extensions import celery
from app.utils import BinanceHelper
import logging
from decimal import Decimal

logger = logging.getLogger('main_logger')
error_logger = logging.getLogger('error_logger')

@celery.task(name='handle_enter_trade')
def handle_enter_trade_task(payload):
    try:
        bh = BinanceHelper()
        bh.handle_enter_trade(payload)
        logger.info("Switch trade executed via Celery")
    except Exception as e:
        error_logger.error(f"Switch trade task failed: {e}")


@celery.task(name='handle_exit_trade')
def handle_exit_trade_task(payload):
    try:
        bh = BinanceHelper()
        bh.handle_exit_trade(payload)
        logger.info("Exit trade executed via Celery")
    except Exception as e:
        error_logger.error(f"Exit trade task failed: {e}")
