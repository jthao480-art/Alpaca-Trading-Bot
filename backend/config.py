from dotenv import load_dotenv
load_dotenv()

import os

# ===========================
# Alpaca API credentials
# ===========================
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")

# Alpaca API URLs
ALPACA_BASE_URL = (
    os.getenv("ALPACA_BASE_URL")
    or os.getenv("APCA_API_BASE_URL")
    or "https://paper-api.alpaca.markets"
)

ALPACA_DATA_BASE_URL = (
    os.getenv("ALPACA_DATA_BASE_URL")
    or "https://data.alpaca.markets"
)

ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED") or "iex"

# Validate credentials on import
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise ValueError(
        "Missing Alpaca credentials. Set environment variables:\n"
        "  ALPACA_API_KEY (or APCA_API_KEY_ID)\n"
        "  ALPACA_SECRET_KEY (or APCA_API_SECRET_KEY)"
    )

# ===========================
# Trading settings
# ===========================
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
PAPER_TRADING = "paper" in ALPACA_BASE_URL.lower()

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "10000"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "10"))
DAILY_LOSS_LIMIT_USD = float(os.getenv("DAILY_LOSS_LIMIT_USD", "500"))

# Symbols to trade
SYMBOLS = os.getenv("SYMBOLS", "AAPL,MSFT,TSLA").split(",")
SYMBOLS = [s.strip().upper() for s in SYMBOLS if s.strip()]

# Agent weights for decision scoring
AGENT_WEIGHTS = {
    "news": 0.15,
    "wallet": 0.10,
    "momentum": 0.25,
    "volume": 0.20,
    "forecast": 0.20,
    "fundamentals": 0.10,
}

# Thresholds for trading decisions
BUY_THRESHOLD = 0.6
SELL_THRESHOLD = 0.4

# ===========================
# Database settings
# ===========================
DB_PATH = os.getenv("DB_PATH", "trading_bot.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
STATE_PATH = os.getenv("STATE_PATH", "bot_state.json")

# ===========================
# API / Server settings
# ===========================
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8010"))

# ===========================
# WebSocket settings
# ===========================
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8011"))

# ===========================
# Logging settings
# ===========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ===========================
# Model / Learning settings
# ===========================
MODEL_PATH = os.getenv("MODEL_PATH", "models/trading_model.pkl")

# Compatibility aliases
MAX_POSITION_SIZE_USD = MAX_POSITION_SIZE
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "100000"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.02"))
MAX_SPREAD_PCT = MAX_SPREAD
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.03"))
TAKE_PROFIT_PCT = TAKE_PROFIT
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.015"))
STOP_LOSS_PCT = STOP_LOSS