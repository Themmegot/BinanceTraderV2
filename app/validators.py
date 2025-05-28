from pydantic import BaseModel, Field, ValidationError, model_validator
from typing import Optional, Literal


class StrategyData(BaseModel):
    order_id: str
    order_action: str  # We'll normalize this below

    @model_validator(mode="after")
    def normalize_order_action(cls, data: "StrategyData") -> "StrategyData":
        action = data.order_action.upper()
        if action == "FLAT":
            data.order_action = "EXIT"
        elif action not in {"BUY", "SELL", "EXIT"}:
            raise ValueError(f"Invalid order_action: {action}")
        return data

class BarData(BaseModel):
    order_price: float


class WebhookData(BaseModel):
    passphrase: str = Field(..., description="Secret key to authenticate the webhook")
    ticker: str = Field(..., description="Symbol of the asset to trade, e.g., BTCUSDT")
    leverage: Optional[float] = Field(None, description="Leverage used for the trade")
    percent_of_equity: Optional[float] = Field(None, description="Percentage of total equity to use for this trade")
    strategy: StrategyData
    bar: Optional[BarData] = Field(None, description="Price-related data for the bar that triggered the trade")
    take_profit_percent: Optional[float] = Field(None, description="Percentage profit at which to close the trade")
    stop_loss_percent: Optional[float] = Field(None, description="Percentage loss at which to close the trade")
    trailing_stop_percentage: Optional[float] = Field(None, description="Trailing stop percentage")

    @model_validator(mode="after")
    def check_required_fields(cls, data: "WebhookData") -> "WebhookData":
        if data.strategy.order_action != "EXIT":
            missing = []
            if data.leverage is None:
                missing.append("leverage")
            if data.percent_of_equity is None:
                missing.append("percent_of_equity")
            if data.bar is None:
                missing.append("bar")
            if missing:
                raise ValueError(f"Missing required fields for non-EXIT action: {', '.join(missing)}")
        return data

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Long trade example",
                    "value": {
                        "passphrase": "your_webhook_passphrase",
                        "ticker": "BTCUSDT",
                        "leverage": 20,
                        "percent_of_equity": 25,
                        "strategy": {
                            "order_id": "Switch Long",
                            "order_action": "BUY"
                        },
                        "bar": {
                            "order_price": 108637.0
                        },
                        "take_profit_percent": 10,
                        "stop_loss_percent": 3,
                        "trailing_stop_percentage": 2
                    }
                },
                {
                    "summary": "Short trade example",
                    "value": {
                        "passphrase": "your_webhook_passphrase",
                        "ticker": "BTCUSDT",
                        "leverage": 50,
                        "percent_of_equity": 10,
                        "strategy": {
                            "order_id": "Switch Short",
                            "order_action": "SELL"
                        },
                        "bar": {
                            "order_price": 108637.0
                        },
                        "take_profit_percent": 15,
                        "stop_loss_percent": 5,
                        "trailing_stop_percentage": 1.5
                    }
                },
                {
                    "summary": "Exit trade example",
                    "value": {
                        "passphrase": "your_webhook_passphrase",
                        "ticker": "BTCUSDT",
                        "strategy": {
                            "order_id": "Exit All",
                            "order_action": "EXIT"
                        }
                    }
                },
                {
                "summary": "Flat order example",
                "value": {
                    "passphrase": "your_webhook_passphrase",
                    "ticker": "BTCUSDT",
                    "strategy": {
                    "order_id": "Flat Everything",
                    "order_action": "FLAT"
                    }
                }
            }
            ]
        }
    }
