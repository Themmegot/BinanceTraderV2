import json
from flask import Blueprint, request, jsonify, render_template
from app.config import Config
from app.logger import configure_logging
from app.validators import WebhookData
from app.tasks import handle_switch_trade_task, handle_exit_trade_task
import logging

bp = Blueprint('trading', __name__)
logger = logging.getLogger('main_logger')
error_logger = logging.getLogger('error_logger')

def log_and_return_error(message, status_code=400):
    error_logger.error(message)
    return jsonify({"code": "error", "message": message}), status_code

@bp.route('/')
def index():
    return render_template('index.html')

@bp.route('/webhook', methods=['POST'])
def webhook():
    logger.info("Webhook endpoint called")

    try:
        data = request.get_json(force=True)
        payload = WebhookData.parse_obj(data)
    except Exception as e:
        error_logger.error(f"Invalid JSON payload or validation failed: {e}")
        return log_and_return_error("Invalid input data.", 422)

    if payload.passphrase != Config.WEBHOOK_PASSPHRASE:
        error_logger.error("Invalid passphrase")
        return log_and_return_error("Invalid passphrase", 401)

    try:
        logger.info(f"Valid webhook request received: {payload.dict()}")

        if payload.strategy.order_id.lower().startswith("switch"):
            handle_switch_trade_task.delay(payload.dict())
            return jsonify({"code": "success", "message": "Switch trade task submitted"}), 202

        elif payload.strategy.order_id.lower().startswith(("exit", "flat")):
            handle_exit_trade_task.delay(payload.dict())
            return jsonify({"code": "success", "message": "Exit trade task submitted"}), 202

        else:
            error_logger.error("Invalid strategy order_id prefix")
            return log_and_return_error("Invalid strategy order_id prefix.", 400)

    except Exception as e:
        error_logger.error(f"Unhandled error during webhook processing: {e}")
        return log_and_return_error("Internal server error", 500)
