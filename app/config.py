import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    USE_TESTNET = os.getenv('USE_TESTNET', 'False').lower() == 'true'

    if USE_TESTNET:
        API_KEY = os.getenv('API_KEY_TEST')
        API_SECRET = os.getenv('API_SECRET_TEST')
    else:
        API_KEY = os.getenv('API_KEY')
        API_SECRET = os.getenv('API_SECRET')

    WEBHOOK_PASSPHRASE = os.getenv('WEBHOOK_PASSPHRASE')
    BINANCE_TLD = os.getenv('BINANCE_TLD', 'com')
    MIN_NOTIONAL = 5  # Minimum notional value required
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
