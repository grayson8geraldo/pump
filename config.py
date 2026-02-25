"""
Configuration for the Pump & Dump Trading Bot.
Three growth phases with progressive risk management.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class Phase(Enum):
    """Growth phases with different risk profiles."""
    PHASE_1 = 1  # $100 → $1,000
    PHASE_2 = 2  # $1,000 → $10,000
    PHASE_3 = 3  # $10,000 → $100,000


@dataclass
class PhaseConfig:
    """Risk parameters for each growth phase."""
    name: str
    start_balance: float
    target_balance: float
    leverage: int
    margin_pct: float          # % of deposit per trade
    max_stop_pct: float        # max loss per trade as % of deposit
    daily_stop_pct: float      # max daily loss as % of deposit
    tp_price_pct: float        # take-profit in price movement %
    sl_price_pct: float        # stop-loss in price movement %
    max_concurrent_trades: int
    use_averaging: bool
    avg_margin_pct: float      # margin per averaging entry
    avg_max_entries: int       # max averaging entries
    avg_step_pct: float        # distance between averaging entries (price %)
    avg_tp_roi_pct: float      # TP as ROI % for averaging strategy


# Phase 1: Aggressive growth $100 → $1,000
PHASE_1_CONFIG = PhaseConfig(
    name="Phase 1: $100 → $1,000",
    start_balance=100.0,
    target_balance=1000.0,
    leverage=10,
    margin_pct=10.0,
    max_stop_pct=5.0,
    daily_stop_pct=15.0,
    tp_price_pct=4.0,
    sl_price_pct=3.0,
    max_concurrent_trades=3,
    use_averaging=True,
    avg_margin_pct=2.5,
    avg_max_entries=5,
    avg_step_pct=4.0,
    avg_tp_roi_pct=15.0,
)

# Phase 2: Steady growth $1,000 → $10,000
PHASE_2_CONFIG = PhaseConfig(
    name="Phase 2: $1,000 → $10,000",
    start_balance=1000.0,
    target_balance=10000.0,
    leverage=7,
    margin_pct=4.0,
    max_stop_pct=4.0,
    daily_stop_pct=8.0,
    tp_price_pct=2.5,
    sl_price_pct=2.5,
    max_concurrent_trades=2,
    use_averaging=True,
    avg_margin_pct=1.0,
    avg_max_entries=4,
    avg_step_pct=4.0,
    avg_tp_roi_pct=10.0,
)

# Phase 3: Capital preservation $10,000 → $100,000
PHASE_3_CONFIG = PhaseConfig(
    name="Phase 3: $10,000 → $100,000",
    start_balance=10000.0,
    target_balance=100000.0,
    leverage=5,
    margin_pct=3.0,
    max_stop_pct=3.0,
    daily_stop_pct=5.0,
    tp_price_pct=2.0,
    sl_price_pct=2.0,
    max_concurrent_trades=2,
    use_averaging=False,
    avg_margin_pct=0.75,
    avg_max_entries=3,
    avg_step_pct=4.0,
    avg_tp_roi_pct=7.0,
)

PHASE_CONFIGS = {
    Phase.PHASE_1: PHASE_1_CONFIG,
    Phase.PHASE_2: PHASE_2_CONFIG,
    Phase.PHASE_3: PHASE_3_CONFIG,
}


def get_phase_for_balance(balance: float) -> Phase:
    """Determine the current phase based on account balance."""
    if balance < PHASE_2_CONFIG.start_balance:
        return Phase.PHASE_1
    elif balance < PHASE_3_CONFIG.start_balance:
        return Phase.PHASE_2
    else:
        return Phase.PHASE_3


def get_config(balance: float) -> PhaseConfig:
    """Get the appropriate config for the current balance."""
    phase = get_phase_for_balance(balance)
    return PHASE_CONFIGS[phase]


# ── Telegram settings ──
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")

# Channel(s) to monitor for signals
# Can be channel username or ID (negative number)
SIGNAL_CHANNELS = [
    s.strip() for s in os.getenv("SIGNAL_CHANNELS", "cryptoscammmonitoring").split(",")
]

# ── Bybit settings ──
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# ── Dry-run (paper trading) ──
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DRY_RUN_BALANCE = float(os.getenv("DRY_RUN_BALANCE", "100.0"))

# ── General settings ──
# Minimum seconds between processing signals for the same coin
SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "60"))

# Max signals of the same direction within this window = BTC movement, skip
BTC_SPAM_THRESHOLD = int(os.getenv("BTC_SPAM_THRESHOLD", "5"))
BTC_SPAM_WINDOW_SEC = int(os.getenv("BTC_SPAM_WINDOW_SEC", "120"))

# Setup quality minimum to open a trade (1-5 scale)
MIN_SETUP_QUALITY = int(os.getenv("MIN_SETUP_QUALITY", "2"))

# Coins to never trade
BLACKLISTED_COINS = [
    s.strip().upper()
    for s in os.getenv("BLACKLISTED_COINS", "").split(",")
    if s.strip()
]

# Log file
LOG_FILE = os.getenv("LOG_FILE", "trades.log")
DB_FILE = os.getenv("DB_FILE", "trades.db")
