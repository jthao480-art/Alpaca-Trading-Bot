from __future__ import annotations

import os
from functools import lru_cache

from dotenv import dotenv_values
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

# Load exclusively from .env — shell env vars cannot override
import os
_env = {**dotenv_values(".env"), **os.environ}

def _get(key: str, default: str = "") -> str:
    return _env.get(key, default)

ALPACA_API_KEY = _get("ALPACA_API_KEY") or _get("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = _get("ALPACA_SECRET_KEY") or _get("APCA_API_SECRET_KEY")

ALPACA_BASE_URL = (
    _get("ALPACA_BASE_URL")
    or _get("APCA_API_BASE_URL")
    or "https://paper-api.alpaca.markets"
)

ALPACA_DATA_URL = _env.get("ALPACA_DATA_URL", "https://data.alpaca.markets")

PAPER_TRADING = "paper" in ALPACA_BASE_URL.lower()

MIN_VOLUME = int(_env.get("MIN_VOLUME", "100"))
POSITION_SIZE_PCT = float(_env.get("POSITION_SIZE_PCT", "0.10"))  # 10% of buying power max
MAX_POSITION_SIZE_USD = float(_env.get("MAX_POSITION_SIZE_USD", "10000"))  # $10K max per positionBUY_THRESHOLD = float(_env.get("BUY_THRESHOLD", "0.60"))
SELL_THRESHOLD = float(_env.get("SELL_THRESHOLD", "0.40"))
TAKE_PROFIT_PCT = float(_env.get("TAKE_PROFIT_PCT", "0.04"))
STOP_LOSS_PCT = float(_env.get("STOP_LOSS_PCT", "0.06"))
DAILY_LOSS_LIMIT_USD = float(_env.get("DAILY_LOSS_LIMIT_USD", "2000.0"))
DAILY_LOSS_LIMIT = -float(_env.get("DAILY_LOSS_LIMIT_USD", "2000.0"))
HARD_STOP_TRIGGER_PCT = float(_env.get("HARD_STOP_TRIGGER_PCT", "-0.055"))
SESSION_FLATTEN_TIME = _env.get("SESSION_FLATTEN_TIME", "15:45")
USE_ALL_TRADABLE = _env.get("USE_ALL_TRADABLE", "false").lower() == "true"
MAX_LEVERAGE = float(_env.get("MAX_LEVERAGE", "1.5"))
MAX_SHORT_LEVERAGE = float(_env.get("MAX_SHORT_LEVERAGE", "0.5"))
MAX_CONCURRENT_SYMBOLS = int(_env.get("MAX_CONCURRENT_SYMBOLS", "10"))
BATCH_SIZE = int(_env.get("BATCH_SIZE", "25"))
MAX_POSITIONS = int(_env.get("MAX_POSITIONS", "25"))
BUY_POWER_CAP = float(_env.get("BUY_POWER_CAP", "0.20"))
BUY_POWER_CAP_OVERNIGHT = float(_env.get("BUY_POWER_CAP_OVERNIGHT", "0.35"))
BUY_POWER_CAP_EXTENDED = float(_env.get("BUY_POWER_CAP_EXTENDED", "0.25"))
EARLY_ENTRY_THRESHOLD = float(_env.get("EARLY_ENTRY_THRESHOLD", "0.62"))
VOLUME_RATIO_ENTRY = float(_env.get("VOLUME_RATIO_ENTRY", "1.05"))
VOLUME_RATIO_EXIT = float(_env.get("VOLUME_RATIO_EXIT", "0.95"))
USE_WAVE_AGENT = _env.get("USE_WAVE_AGENT", "false").lower() == "true"
USE_ARES_AGENT = _env.get("USE_ARES_AGENT", "false").lower() == "true"
USE_INTRADAY_AGENT = _env.get("USE_INTRADAY_AGENT", "false").lower() == "true"
USE_DEFAULT_AGENTS = _env.get("USE_DEFAULT_AGENTS", "true").lower() == "true"
USE_RIPPLE = _env.get("USE_RIPPLE", "true").lower() == "true"
USE_ARES_PROVISIONAL = _env.get("USE_ARES_PROVISIONAL", "true").lower() == "true"
USE_WAVE_PROVISIONAL = _env.get("USE_WAVE_PROVISIONAL", "true").lower() == "true"
USE_SURGE = _env.get("USE_SURGE", "true").lower() == "true"
LOSER_EXIT_THRESHOLD = float(_env.get("LOSER_EXIT_THRESHOLD", "-0.05"))
DATA_DIR = _env.get("DATA_DIR", ".")
USE_ARES_BEARISH = _env.get("USE_ARES_BEARISH", "false").lower() == "true"
TRADETIQ_API_KEY = _env.get("TRADETIQ_API_KEY", "")
TRADETIQ_BASE_URL = _env.get("TRADETIQ_BASE_URL", "https://tradetiq-production.up.railway.app")
USE_TRADETIQ_AGENT = _env.get("USE_TRADETIQ_AGENT", "false").lower() == "true"
USE_TRADETIQ_RIPPLE = _env.get("USE_TRADETIQ_RIPPLE", "true").lower() == "true"
USE_TRADETIQ_ARES = _env.get("USE_TRADETIQ_ARES", "true").lower() == "true"
USE_TRADETIQ_WAVE = _env.get("USE_TRADETIQ_WAVE", "true").lower() == "true"
USE_TRADETIQ_SMARTTIQ = _env.get("USE_TRADETIQ_SMARTTIQ", "false").lower() == "true"
USE_TRADETIQ_NEXUS = _env.get("USE_TRADETIQ_NEXUS", "false").lower() == "true"

@lru_cache(maxsize=1)
def load_tradable_equities() -> list[str]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []

    client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_TRADING)
    req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = client.get_all_assets(req)

    symbols: list[str] = []
    for asset in assets:
        symbol = getattr(asset, "symbol", None)
        status = getattr(asset, "status", None)
        tradable = bool(getattr(asset, "tradable", False))
        fractionable = bool(getattr(asset, "fractionable", False))

        # Detect warrant/right/unit/preferred suffixes
        _is_special = (
            symbol.endswith("W") and len(symbol) >= 4    # warrants e.g. ACBAW
            or symbol.endswith("R") and len(symbol) >= 4  # rights e.g. ACBAR
            or symbol.endswith("U") and len(symbol) >= 4  # units e.g. ACBAU
            or symbol.endswith("WS") and len(symbol) >= 4 # warrants e.g. ACBAWS
            or "PRN" in symbol                             # preferred notes
        ) if isinstance(symbol, str) else True

        if (
            isinstance(symbol, str)
            and tradable
            and fractionable             # only liquid, commonly-traded stocks
            and status == AssetStatus.ACTIVE
            and "." not in symbol        # exclude symbols like F.PRB, BRK.B
            and "/" not in symbol        # exclude crypto-style symbols
            and len(symbol) <= 5         # exclude long OTC symbols
            and not _is_special          # exclude warrants, rights, units
        ):
            symbols.append(symbol)

    return sorted(symbols)


_env_symbols = [s.strip() for s in _env.get("SYMBOLS", "").split(",") if s.strip()]
SYMBOLS = load_tradable_equities() if USE_ALL_TRADABLE else (_env_symbols or ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA"])
SYMBOL_UNIVERSE = SYMBOLS[:]
