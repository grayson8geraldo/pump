"""
Technical indicators for setup quality assessment.
RSI, Pivot Points Standard, candle pattern detection.
"""

import pandas as pd
import ta


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute RSI from a DataFrame with a 'close' column."""
    return ta.momentum.RSIIndicator(df["close"], window=period).rsi()


def is_overbought(rsi_value: float, threshold: float = 70.0) -> bool:
    return rsi_value >= threshold


def is_oversold(rsi_value: float, threshold: float = 30.0) -> bool:
    return rsi_value <= threshold


# ── Pivot Points Standard (Traditional) ──

def compute_pivot_points(df: pd.DataFrame, period: str = "D") -> dict[str, float]:
    """
    Compute Traditional Pivot Points from OHLCV data.
    Uses the last completed candle of the given period.
    For daily pivots, pass 1h or 1d candles.

    Returns dict with keys: P, R1-R5, S1-S5
    """
    if period == "D":
        # Use the last full day's data
        high = df["high"].max()
        low = df["low"].min()
        close = df["close"].iloc[-1]
    else:
        high = df["high"].max()
        low = df["low"].min()
        close = df["close"].iloc[-1]

    p = (high + low + close) / 3.0

    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    r4 = r3 + (r2 - r1)
    s4 = s3 - (s1 - s2)
    r5 = r4 + (r2 - r1)
    s5 = s4 - (s1 - s2)

    return {
        "P": p,
        "R1": r1, "R2": r2, "R3": r3, "R4": r4, "R5": r5,
        "S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5,
    }


def price_near_pivot(price: float, pivots: dict[str, float], tolerance_pct: float = 0.5) -> str | None:
    """
    Check if price is near any pivot level.
    Returns the level name (e.g. 'R2') or None.
    """
    for name, level in pivots.items():
        if level == 0:
            continue
        distance_pct = abs(price - level) / level * 100
        if distance_pct <= tolerance_pct:
            return name
    return None


# ── Candle patterns ──

def is_long_wick_candle(row: pd.Series, min_wick_ratio: float = 0.6) -> str | None:
    """
    Check if a candle has a long wick (sign of manipulation).
    Returns 'upper' if long upper wick, 'lower' if long lower wick, None otherwise.

    A long upper wick means: (high - max(open, close)) / (high - low) >= ratio
    A long lower wick means: (min(open, close) - low) / (high - low) >= ratio
    """
    body_range = row["high"] - row["low"]
    if body_range == 0:
        return None

    upper_wick = row["high"] - max(row["open"], row["close"])
    lower_wick = min(row["open"], row["close"]) - row["low"]

    if upper_wick / body_range >= min_wick_ratio:
        return "upper"
    if lower_wick / body_range >= min_wick_ratio:
        return "lower"
    return None


def is_engulfing(df: pd.DataFrame) -> str | None:
    """
    Check the last two candles for an engulfing pattern.
    Returns 'bearish' or 'bullish' or None.
    """
    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(curr["close"] - curr["open"])

    if curr_body <= prev_body:
        return None

    # Bullish engulfing: prev is red, curr is green and engulfs
    if prev["close"] < prev["open"] and curr["close"] > curr["open"]:
        if curr["open"] <= prev["close"] and curr["close"] >= prev["open"]:
            return "bullish"

    # Bearish engulfing: prev is green, curr is red and engulfs
    if prev["close"] > prev["open"] and curr["close"] < curr["open"]:
        if curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
            return "bearish"

    return None


def detect_reversal_candle(df: pd.DataFrame) -> str | None:
    """
    Detect if the most recent candle is a reversal signal.
    Returns 'bullish' or 'bearish' or None.
    """
    if len(df) < 2:
        return None

    last = df.iloc[-1]

    # Check for pin bar / long wick
    wick = is_long_wick_candle(last)
    if wick == "upper":
        return "bearish"
    if wick == "lower":
        return "bullish"

    # Check engulfing
    eng = is_engulfing(df)
    if eng:
        return eng

    return None


def is_trending(df: pd.DataFrame, lookback: int = 10, min_consecutive: int = 5) -> str | None:
    """
    Detect if price is in a sustained trend (multiple consecutive candles).
    Returns 'up', 'down', or None.
    """
    if len(df) < lookback:
        return None

    recent = df.tail(lookback)
    green = 0
    red = 0
    for _, row in recent.iterrows():
        if row["close"] > row["open"]:
            green += 1
        elif row["close"] < row["open"]:
            red += 1

    if green >= min_consecutive:
        return "up"
    if red >= min_consecutive:
        return "down"
    return None


def compute_volume_spike(df: pd.DataFrame, lookback: int = 20) -> float:
    """
    Calculate how much the latest volume exceeds the average.
    Returns ratio (e.g. 3.0 means 3x average volume).
    """
    if len(df) < lookback + 1:
        return 1.0

    avg_vol = df["volume"].iloc[-(lookback + 1):-1].mean()
    if avg_vol == 0:
        return 1.0

    return df["volume"].iloc[-1] / avg_vol
