# BinanceTrader 🧠📈

A lightweight, containerized, Flask + Celery-based webhook trading system designed for algorithmic strategies on Binance via TradingView alerts. Built to run on a Raspberry Pi or any Docker-compatible system.

## 🌟 Features

- 🔐 Webhook-secured strategy execution
- 🔄 Long and short entries with full exit logic
- 🧮 Leverage, equity percent, and price control per trade
- 🛑 Integrated SL/TP and trailing stop mechanisms
- 🧾 Automatic transaction logging (`transactions.csv`)
- 📊 Logging system with persistent `info.log` and `errors.log`
- ⚙️ Configurable via `.env` file
- 🐳 Dockerized: run anywhere, reliably
- 🧪 Testnet support with `USE_TESTNET`
- 🧰 Built-in input validation with Pydantic
- 🔁 Redis + Celery asynchronous task queuing
- 💼 Production-ready with Gunicorn

---

## 🗂 Project Structure

```
BinanceTraderV2/
│
├── app/                   # Flask app and business logic
│   ├── __init__.py        # Flask app factory
│   ├── config.py          # Configuration loader
│   ├── extensions.py      # Celery, Redis, Logging setup
│   ├── logger.py          # Logging configuration
│   ├── routes.py          # Webhook endpoint
│   ├── tasks.py           # Celery tasks (enter/exit)
│   ├── utils.py           # Binance trade logic & transaction CSV writer
│   ├── validators.py      # Pydantic validation models
│   └── templates/
│       └── index.html     # Basic index page
│
├── redis.conf             # Custom Redis config
├── run.py                 # Entry point for Gunicorn + Flask
├── celery_worker.py       # Celery app entry point
├── requirements.txt       # Python dependencies
├── Dockerfile             # Docker build
├── docker-compose.yml     # Container stack
├── env.example            # Example .env file
└── README.md              # You're here :)
```

---

## ⚙️ Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/BinanceTraderV2.git
cd BinanceTraderV2
```

### 2. Configure your `.env`

Copy `env.example` to `.env` and fill in:

```env
API_KEY=your_api_key
API_SECRET=your_api_secret
USE_TESTNET=true
WEBHOOK_PASSPHRASE=your_webhook_pass
```

### 3. Start the application

```bash
sudo docker-compose up --build -d
```

### 4. Access the server

- Webhook endpoint: `http://<your-ip>:5001/webhook`
- Index page: `http://<your-ip>:5001/`

---

## 📤 Webhook Payload Format

Send POST requests from TradingView using this schema:

```json
{
  "passphrase": "your_webhook_pass",
  "ticker": "{{ticker}} ",
  "leverage": 20,
  "percent_of_equity": 25,
  "strategy": {
    "order_id": "{{strategy.market_position}}",
    "order_action": "{{strategy.order.action}}"
  },
  "bar": {
    "order_price": "{{close}}"
  },
  "take_profit_percent": 10,
  "stop_loss_percent": 3,
  "trailing_stop_percentage": 2
}
```

- `order_action`: `"BUY"`, `"SELL"`, `"EXIT"`, or `"FLAT"` (alias for exit)
- `EXIT` requires only `passphrase`, `ticker`, and `strategy`

---

## 📦 Volumes and Logging

All persistent logs and CSVs are written to `./logs/`:

- `logs/info.log` — General logs
- `logs/errors.log` — Errors
- `logs/transactions.csv` — Trade history

You can optionally mount the `logs/` directory for persistence across container rebuilds.

---

## 🔒 Security

- Webhook access protected by a shared secret (`passphrase`)
- Redis is bound to `127.0.0.1`
- Dangerous Redis commands disabled in `redis.conf`

---

## 🧠 Tips

- Set up `logrotate` to prevent uncontrolled growth of logs
- Use `--uid` option in Docker or avoid running as root for Celery
- Production settings already use Gunicorn for `web` service

---

## 🐳 Docker Tips

- View logs: `docker-compose logs -f`
- Restart stack: `docker-compose restart`
- Stop all: `docker-compose down`
- Monitor containers: `docker ps`

---

## ✨ Credits

Built with ❤️ by Børge and ChatGPT — a brilliant blend of human logic and AI precision.

---

## 📜 License

MIT — feel free to use, fork, improve!
