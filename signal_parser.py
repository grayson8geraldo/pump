"""
Parser for Pump & Dump Screener signals from Telegram.

Signal format:
  🟢 COIN +X.XX%   (pump)
  🔴 COIN -X.XX%   (dump)
  Hashtags: #Bybit #Binance etc.
  🔔N  (signal count for the day)
"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalDirection(Enum):
    PUMP = "pump"   # 🟢
    DUMP = "dump"   # 🔴


@dataclass
class Signal:
    """Parsed trading signal from the screener."""
    ticker: str
    direction: SignalDirection
    change_pct: float
    exchanges: list[str] = field(default_factory=list)
    bell_count: int = 1          # 🔔 N — how many signals today
    timestamp: float = 0.0
    raw_text: str = ""

    @property
    def is_pump(self) -> bool:
        return self.direction == SignalDirection.PUMP

    @property
    def is_dump(self) -> bool:
        return self.direction == SignalDirection.DUMP

    @property
    def abs_change(self) -> float:
        return abs(self.change_pct)


# Patterns
_GREEN = re.compile(r"🟢|✅|GREEN", re.IGNORECASE)
_RED = re.compile(r"🔴|❌|RED", re.IGNORECASE)
_TICKER = re.compile(r"#?([A-Z0-9]{2,15})(?:USDT)?", re.IGNORECASE)
_PCT = re.compile(r"[+\-]?\s*(\d+(?:[.,]\d+)?)\s*%")
_BELL = re.compile(r"🔔\s*(\d+)")
_EXCHANGE_TAGS = re.compile(
    r"#(Bybit|Binance|OKX|MEXC|Bitget|Bingx|Gate)", re.IGNORECASE
)

# Known exchange names that may appear without hashtag
_EXCHANGE_NAMES = {"bybit", "binance", "okx", "mexc", "bitget", "bingx", "gate"}

# Words that are clearly NOT tickers
_NOT_TICKER = {
    "USDT", "BTC", "ETH", "USD", "ROI", "PUMP", "DUMP", "SCREENER",
    "GREEN", "RED", "TREND", "MARKET", "GUIDE", "BYBIT", "BINANCE",
    "OKX", "MEXC", "BITGET", "BINGX", "GATE", "THE", "AND", "FOR",
}


def parse_signal(text: str) -> Optional[Signal]:
    """
    Parse a Telegram message into a Signal object.
    Returns None if the message is not a valid screener signal.
    """
    if not text or len(text) < 5:
        return None

    # Determine direction
    has_green = bool(_GREEN.search(text))
    has_red = bool(_RED.search(text))

    if has_green == has_red:
        # Either both or neither — not a clear signal
        # Try text-based detection
        text_lower = text.lower()
        if "восходящий" in text_lower or "uptrend" in text_lower:
            return None  # Trend notification, not a trade signal
        if "нисходящий" in text_lower or "downtrend" in text_lower:
            return None
        return None

    direction = SignalDirection.PUMP if has_green else SignalDirection.DUMP

    # Extract percentage change
    pct_match = _PCT.search(text)
    if not pct_match:
        return None
    change_str = pct_match.group(1).replace(",", ".")
    change_pct = float(change_str)
    if direction == SignalDirection.DUMP:
        change_pct = -change_pct

    # Extract ticker — find word candidates
    # Take the first uppercase token that looks like a ticker
    ticker = None
    # Try explicit #TICKER pattern first
    for m in _TICKER.finditer(text):
        candidate = m.group(1).upper()
        if candidate not in _NOT_TICKER and candidate not in _EXCHANGE_NAMES:
            ticker = candidate
            break

    if not ticker:
        # Fallback: look for uppercase words 2-10 chars
        words = re.findall(r"\b([A-Z][A-Z0-9]{1,9})\b", text)
        for w in words:
            if w not in _NOT_TICKER and w.lower() not in _EXCHANGE_NAMES:
                ticker = w
                break

    if not ticker:
        return None

    # Clean up ticker — remove trailing "USDT" if present
    if ticker.endswith("USDT"):
        ticker = ticker[:-4]

    # Extract exchanges
    exchanges = [m.group(1).capitalize() for m in _EXCHANGE_TAGS.finditer(text)]
    if not exchanges:
        # Try plain text mentions
        for name in _EXCHANGE_NAMES:
            if name in text.lower():
                exchanges.append(name.capitalize())

    # Extract bell count
    bell_match = _BELL.search(text)
    bell_count = int(bell_match.group(1)) if bell_match else 1

    return Signal(
        ticker=ticker,
        direction=direction,
        change_pct=change_pct,
        exchanges=exchanges,
        bell_count=bell_count,
        timestamp=time.time(),
        raw_text=text,
    )


def is_btc_trend_signal(text: str) -> bool:
    """Check if the message is a BTC trend notification (not a trade signal)."""
    text_lower = text.lower()
    return (
        "btc" in text_lower
        and ("тренд" in text_lower or "trend" in text_lower)
    )


def is_spam_wave(signals: list[Signal], window_sec: int, threshold: int) -> bool:
    """
    Detect if we're in a BTC-driven spam wave.
    Returns True if more than `threshold` signals of the same direction
    arrived within `window_sec`.
    """
    if len(signals) < threshold:
        return False

    now = time.time()
    recent = [s for s in signals if now - s.timestamp <= window_sec]

    if len(recent) < threshold:
        return False

    pump_count = sum(1 for s in recent if s.is_pump)
    dump_count = sum(1 for s in recent if s.is_dump)

    return pump_count >= threshold or dump_count >= threshold
