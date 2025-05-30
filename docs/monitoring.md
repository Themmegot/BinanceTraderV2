# Monitoring & Debugging `BinanceTrader` 🚦

This document outlines how to verify the containerized application is correctly receiving, parsing, and acting on signals from TradingView webhooks.

---

## 🔁 End-to-End Checklist

### 1. Webhook Reception
- Monitor logs:
  ```bash
  docker-compose logs -f web
  ```
- Look for:
  ```log
  [INFO] Webhook endpoint called
  ```

### 2. JSON Payload Validation
- Example test command (replace `your-secret-passphrase`):
  ```bash
  curl -X POST http://localhost:5001/webhook \
    -H "Content-Type: application/json" \
    -d '{
          "passphrase": "your-secret-passphrase",
          "ticker": "BTCUSDT",
          "strategy": "Supertrend",
          "side": "long",
          "strategy_id": "tv_test_001"
        }'
  ```
- Make sure all **required fields** match the `WebhookData` schema in `validators.py`.

### 3. Worker Task Dispatch
- Run:
  ```bash
  docker-compose logs -f worker
  ```
- Check for task acknowledgment:
  ```log
  Task handle_enter_trade[...] received
  ```

### 4. Trade Execution via Binance
- Confirm trade steps in `logs/info.log`:
  ```log
  Placing order...
  Market order placed successfully.
  ```

### 5. Result Logging
- Watch these files (mounted to host via `logs/` folder):
  - `logs/info.log` → General activity
  - `logs/errors.log` → Validation or trade errors
  - `logs/transactions.csv` → All trade records

---

## 📌 Common Errors

### ❌ ValidationError on Webhook
Check this log:
```log
Invalid JSON payload or validation failed: 6 validation errors for WebhookData
```
- Double-check field names and remove extra keys.
- The model only allows: `passphrase`, `ticker`, `strategy`, `side`, and `strategy_id`.

### ❌ Redis Unavailable
```log
Cannot connect to redis://redis:6379/0
```
- Make sure Redis container is up and not crashing due to misconfiguration in `redis.conf`.

---

Built with ❤️ by Børge and ChatGPT — a brilliant blend of human logic and AI precision.