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
MIN_CASH_RESERVE = float(_env.get("MIN_CASH_RESERVE", "0.0"))
HARD_STOP_TRIGGER_PCT = float(_env.get("HARD_STOP_TRIGGER_PCT", "-0.055"))
# Floor (in percent, e.g. 1.0 = 1%) under which a trade's trailing stop can
# never be set, regardless of how tight its momentum-scaled stop_loss_pct is
# (see build_exit_plan in botv3.py). Low-momentum signals were getting a
# trailing stop as tight as 0.5%, which is within normal bid-ask noise for a
# volatile ticker and gets clipped almost immediately regardless of whether
# the signal's direction was right. Set to 0 to disable the floor.
TRAILING_STOP_FLOOR_PCT = float(_env.get("TRAILING_STOP_FLOOR_PCT", "1.0"))
# Minimum trade_priority-score edge a new candidate must have over the open
# position with the LOWEST stored priority score before the bot will sell
# that position early to free a slot once MAX_POSITIONS is reached. Keeps
# rotation reserved for a clearly better signal instead of firing on any
# marginal edge, which would just add round-trip slippage. 0 rotates on any
# improvement at all.
ROTATION_PRIORITY_MARGIN = float(_env.get("ROTATION_PRIORITY_MARGIN", "0.15"))
# A held position already up more than this (unrealized P&L %, e.g. 0.015 =
# 1.5%) is never a rotation target, even if it has the lowest stored
# priority score. A stale, low entry-time score isn't a reason to cut a
# position that's actually working — its own take-profit/trailing-stop
# stays in charge of that exit.
ROTATION_MAX_WINNER_PLPC = float(_env.get("ROTATION_MAX_WINNER_PLPC", "0.015"))
# Caps how many existing positions can be rotated out in a single scan cycle
# once the book is full, so one cycle can't unwind a large chunk of the
# portfolio chasing whatever signals happen to fire that pass.
MAX_ROTATIONS_PER_CYCLE = int(_env.get("MAX_ROTATIONS_PER_CYCLE", "2"))
# ── Held-position news watcher (backend/news_watch.py) ───────────────────────
# Exit-only: polls Alpaca news every NEWS_WATCH_INTERVAL_SECONDS for the symbols
# currently held (04:00-20:00 ET weekdays, so premarket/after-hours included) and
# exits a long on strongly NEGATIVE news / a short on strongly POSITIVE news.
NEWS_WATCH_ENABLED = _env.get("NEWS_WATCH_ENABLED", "true").lower() == "true"
# true = log what it WOULD do but place no orders.
NEWS_WATCH_DRY_RUN = _env.get("NEWS_WATCH_DRY_RUN", "false").lower() == "true"
NEWS_WATCH_INTERVAL_SECONDS = float(_env.get("NEWS_WATCH_INTERVAL_SECONDS", "60"))
# On the first poll after a restart, how far back to look for unseen articles.
NEWS_WATCH_LOOKBACK_MINUTES = float(_env.get("NEWS_WATCH_LOOKBACK_MINUTES", "30"))
# FinBERT confidence needed (when no catastrophic phrase matched) to act.
NEWS_WATCH_MIN_CONF = float(_env.get("NEWS_WATCH_MIN_CONF", "0.90"))
# Articles tagging more than this many symbols are market roundups — ignored.
NEWS_WATCH_MAX_TAGGED = int(_env.get("NEWS_WATCH_MAX_TAGGED", "4"))
# Extended-hours exit: marketable limit this far through the last price, widened
# (x2, x3, x4) each time it's repriced; converted to a market exit at the open.
NEWS_EXIT_LIMIT_SLIP_PCT = float(_env.get("NEWS_EXIT_LIMIT_SLIP_PCT", "0.03"))
NEWS_EXIT_REPRICE_SECONDS = float(_env.get("NEWS_EXIT_REPRICE_SECONDS", "120"))
# "Let it run" trailing-stop ratchet. A position's trailing stop starts at
# the tight momentum-scaled width from build_exit_plan (0.5-1.5%, floored by
# TRAILING_STOP_FLOOR_PCT) — sized for the signal's risk AT ENTRY, and it
# never adjusted for how the trade actually performed afterward, so a
# position that took off got cut on the same small pullback that would've
# stopped out a signal that went nowhere. Once unrealized gain crosses one
# of these thresholds, the trailing stop widens to the paired percentage
# instead of staying pinned at its starting width — a proven runner gets
# more room, while a flat or losing position is left untouched. Format:
# "gain1:trail1,gain2:trail2,..." (gain as a fraction, e.g. 0.03 = 3%; trail
# as a percent, e.g. 2.0 = 2.0%). Each trail stays well under its gain
# threshold so even a full pullback to the new stop still locks in real
# profit — this only ever widens a stop, never tightens one. The position's
# own hold-period exit and volume-fade exit are untouched by this and can
# still close it regardless. Set to "" to disable.
TRAILING_STOP_RATCHET = _env.get("TRAILING_STOP_RATCHET", "0.03:2.0,0.06:3.5,0.10:5.0")
SESSION_FLATTEN_TIME = _env.get("SESSION_FLATTEN_TIME", "15:45")
USE_ALL_TRADABLE = _env.get("USE_ALL_TRADABLE", "false").lower() == "true"
MAX_LEVERAGE = float(_env.get("MAX_LEVERAGE", "1.5"))
MAX_SHORT_LEVERAGE = float(_env.get("MAX_SHORT_LEVERAGE", "0.5"))
# Master switch for OPENING new shorts. Paper bot keeps shorting by default; set
# ENABLE_SHORTS=false in Railway to turn it off. Existing shorts are still protected/covered.
ENABLE_SHORTS = _env.get("ENABLE_SHORTS", "true").lower() == "true"
MAX_CONCURRENT_SYMBOLS = int(_env.get("MAX_CONCURRENT_SYMBOLS", "10"))
BATCH_SIZE = int(_env.get("BATCH_SIZE", "25"))
MAX_POSITIONS = int(_env.get("MAX_POSITIONS", "25"))
MIN_PRICE = float(_env.get("MIN_PRICE", "4.0"))
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
# Bundled family switches — kept only as the fallback default for the split
# EOD/PROVISIONAL toggles below, so an existing deployment that has never set
# the new variables keeps behaving exactly as it does today.
USE_TRADETIQ_RIPPLE = _env.get("USE_TRADETIQ_RIPPLE", "true").lower() == "true"
USE_TRADETIQ_ARES = _env.get("USE_TRADETIQ_ARES", "true").lower() == "true"
USE_TRADETIQ_WAVE = _env.get("USE_TRADETIQ_WAVE", "true").lower() == "true"
# Independent EOD vs provisional control per signal family. Each defaults to
# the family's bundled switch above, so setting only USE_TRADETIQ_WAVE (say)
# still controls both; setting USE_TRADETIQ_WAVE_PROVISIONAL explicitly
# overrides just that side, letting EOD and provisional run independently.
USE_TRADETIQ_RIPPLE_EOD = _env.get("USE_TRADETIQ_RIPPLE_EOD", "true" if USE_TRADETIQ_RIPPLE else "false").lower() == "true"
USE_TRADETIQ_RIPPLE_PROVISIONAL = _env.get("USE_TRADETIQ_RIPPLE_PROVISIONAL", "true" if USE_TRADETIQ_RIPPLE else "false").lower() == "true"
USE_TRADETIQ_ARES_EOD = _env.get("USE_TRADETIQ_ARES_EOD", "true" if USE_TRADETIQ_ARES else "false").lower() == "true"
USE_TRADETIQ_ARES_PROVISIONAL = _env.get("USE_TRADETIQ_ARES_PROVISIONAL", "true" if USE_TRADETIQ_ARES else "false").lower() == "true"
USE_TRADETIQ_WAVE_EOD = _env.get("USE_TRADETIQ_WAVE_EOD", "true" if USE_TRADETIQ_WAVE else "false").lower() == "true"
USE_TRADETIQ_WAVE_PROVISIONAL = _env.get("USE_TRADETIQ_WAVE_PROVISIONAL", "true" if USE_TRADETIQ_WAVE else "false").lower() == "true"
# SmartTiq/Nexus have no provisional variant in Tradetiq's payload — EOD only.
USE_TRADETIQ_SMARTTIQ = _env.get("USE_TRADETIQ_SMARTTIQ", "false").lower() == "true"
USE_TRADETIQ_NEXUS = _env.get("USE_TRADETIQ_NEXUS", "false").lower() == "true"
DISCORD_WEBHOOK_URL = _env.get("DISCORD_WEBHOOK_URL", "")

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
