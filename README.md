### 1. README.md (Docker Deployment)

```markdown
# Binance Flask Trading Webhook

This project is a Flask-based webhook service connected to Binance for automated trading. It uses Celery for background job processing and Docker Compose for container orchestration.

## Features
- Flask API with `/webhook` route
- Pydantic validation of trading signals
- Celery for background task handling
- Redis as Celery broker
- Dockerized deployment

---

## 📦 Project Structure

```
.
├── app/
│   ├── __init__.py
│   ├── routes.py
│   ├── validators.py
│   ├── binance_utils.py
│   └── templates/
│       └── index.html
├── celery_worker.py
├── run.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env
```

---

## 🚀 Quickstart (Docker)

1. **Clone the repo**
```bash
git clone https://github.com/your-repo/binance-trader
cd binance-trader
```

2. **Set environment variables**
```bash
cp .env.example .env
# Then edit .env with your real credentials
```

3. **Build and start the containers**
```bash
docker compose up --build
```

4. **Access the web interface**
```
http://localhost:5000
```

5. **Test webhook** (e.g., via Insomnia or curl)
```
POST http://localhost:5000/webhook
Content-Type: application/json

{
  "passphrase": "example_passphrase",
  "ticker": "BTCUSDT",
  "leverage": 50,
  "percent_of_equity": 10,
  "strategy": {
    "order_id": "Long Entry",
    "order_action": "BUY"
  },
  "bar": {
    "order_price": 108500.0
  },
  "take_profit_percent": 15,
  "stop_loss_percent": 5,
  "trailing_stop_percentage": 2
}
```

---

## 🧪 Testing Celery
Ensure the worker starts correctly:
```bash
docker compose logs -f worker
```

---

## 🛠️ Developer Notes
- The `validators.py` file contains the Pydantic models used to validate incoming webhook data.
- `binance_utils.py` contains the Binance trading logic.

---

## 🔐 Security
Make sure your `.env` file is not committed to version control!
Add to `.gitignore`:
```
.env
```
```

---

### 2. requirements.txt

```txt
flask==3.0.2
python-dotenv==1.0.1
pydantic==2.6.4
celery==5.3.6
redis==5.0.4
requests==2.31.0
```

---

### 3. .env.example

```env
# Binance API configuration
USE_TESTNET=true

# Testnet keys
API_KEY_TEST=your_testnet_api_key
API_SECRET_TEST=your_testnet_api_secret

# Mainnet keys (used when USE_TESTNET=false)
API_KEY=your_mainnet_api_key
API_SECRET=your_mainnet_api_secret

# Webhook passphrase for validation
WEBHOOK_PASSPHRASE=example_passphrase

# Optional settings
BINANCE_TLD=com
FLASK_DEBUG=true

# Celery / Redis broker
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

Let me know if you'd like help with GitHub setup, CI/CD, or exposing this securely online (e.g., using ngrok or NGINX + SSL).
