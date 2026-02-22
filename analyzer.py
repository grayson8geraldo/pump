"""
Signal analyzer — evaluates each signal and determines:
1. Setup quality (Tier S/A/B/C/D → score 5/4/3/2/1)
2. Whether to use Reverse or Continuation strategy
3. Entry, SL, TP parameters
"""

import logging
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from signal_parser import Signal, SignalDirection
from indicators import (
    compute_rsi,
    is_overbought,
    is_oversold,
    compute_pivot_points,
    price_near_pivot,
    is_long_wick_candle,
    detect_reversal_candle,
    is_trending,
    compute_volume_spike,
)

logger = logging.getLogger(__name__)


class Strategy(Enum):
    REVERSE = "reverse"        # 80% — short pumps, long dumps
    CONTINUATION = "continuation"  # 20% — follow the trend
    SKIP = "skip"              # don't trade this signal


@dataclass
class Analysis:
    """Result of analyzing a signal against the chart."""
    signal: Signal
    strategy: Strategy
    quality: int               # 1-5 (maps to Tier D-S)
    trade_side: str            # 'buy' (long) or 'sell' (short)
    entry_price: float
    sl_price: float
    tp_price: float
    rsi: float
    near_pivot: str | None
    has_reversal_candle: bool
    is_manipulation: bool      # single candle with long wick
    is_sustained_trend: bool
    volume_spike: float
    reasons: list[str]


def analyze_signal(
    signal: Signal,
    df_1m: pd.DataFrame,
    df_1h: pd.DataFrame,
    current_price: float,
    sl_pct: float = 3.0,
    tp_pct: float = 3.0,
    force_strategy: Strategy | None = None,
) -> Analysis:
    """
    Analyze a signal against chart data and return trading parameters.

    df_1m: 1-minute OHLCV (200 candles)
    df_1h: 1-hour OHLCV (200 candles)
    """
    reasons = []
    quality = 0

    # ── 1. RSI ──
    rsi_series = compute_rsi(df_1m, period=14)
    rsi_val = rsi_series.iloc[-1] if len(rsi_series) > 0 else 50.0

    rsi_confirms = False
    if signal.is_pump and is_overbought(rsi_val):
        rsi_confirms = True
        quality += 1
        reasons.append(f"RSI overbought ({rsi_val:.1f})")
    elif signal.is_dump and is_oversold(rsi_val):
        rsi_confirms = True
        quality += 1
        reasons.append(f"RSI oversold ({rsi_val:.1f})")

    # ── 2. Pivot Points ──
    pivots = compute_pivot_points(df_1h)
    near_piv = price_near_pivot(current_price, pivots, tolerance_pct=0.5)
    if near_piv:
        quality += 1
        reasons.append(f"Near pivot level {near_piv}")

    # ── 3. Candle character — manipulation vs trend ──
    last_candle = df_1m.iloc[-1]
    wick = is_long_wick_candle(last_candle)
    is_manip = False

    if signal.is_pump and wick == "upper":
        is_manip = True
        quality += 1
        reasons.append("Long upper wick (manipulation)")
    elif signal.is_dump and wick == "lower":
        is_manip = True
        quality += 1
        reasons.append("Long lower wick (manipulation)")

    # ── 4. Reversal candle ──
    rev = detect_reversal_candle(df_1m)
    has_rev = False
    if signal.is_pump and rev == "bearish":
        has_rev = True
        quality += 1
        reasons.append("Bearish reversal candle")
    elif signal.is_dump and rev == "bullish":
        has_rev = True
        quality += 1
        reasons.append("Bullish reversal candle")

    # ── 5. Trend detection ──
    trend = is_trending(df_1m, lookback=15, min_consecutive=7)
    is_sust_trend = False
    if trend:
        is_sust_trend = True
        reasons.append(f"Sustained {trend} trend on 1m")

    # Also check 1h trend
    trend_1h = is_trending(df_1h, lookback=10, min_consecutive=5)
    if trend_1h:
        is_sust_trend = True
        reasons.append(f"Sustained {trend_1h} trend on 1h")

    # ── 6. Volume spike ──
    vol_spike = compute_volume_spike(df_1m)
    if vol_spike >= 3.0:
        reasons.append(f"Volume spike {vol_spike:.1f}x")

    # ── 7. Signal volatility ──
    high_volatility = signal.abs_change >= 5.0
    extreme_volatility = signal.abs_change >= 20.0

    if high_volatility:
        reasons.append(f"High volatility ({signal.abs_change:.1f}%)")
    if extreme_volatility:
        reasons.append(f"EXTREME volatility ({signal.abs_change:.1f}%)")

    # ── 8. Bell count (signal series) ──
    series_signal = signal.bell_count >= 3
    if series_signal:
        reasons.append(f"Signal series (🔔{signal.bell_count})")

    # ── Determine strategy ──

    # Continuation indicators
    continuation_score = 0
    if is_sust_trend:
        continuation_score += 2
    if high_volatility:
        continuation_score += 1
    if extreme_volatility:
        continuation_score += 2
    if series_signal:
        continuation_score += 1
    # Trend direction matches signal direction
    if signal.is_pump and trend == "up":
        continuation_score += 1
    if signal.is_dump and trend == "down":
        continuation_score += 1

    if force_strategy is not None:
        strategy = force_strategy
        reasons.append(f"→ {strategy.value.upper()} strategy (forced)")
    elif continuation_score >= 3:
        strategy = Strategy.CONTINUATION
        reasons.append(f"→ CONTINUATION strategy (score={continuation_score})")
    else:
        strategy = Strategy.REVERSE
        reasons.append(f"→ REVERSE strategy (score={continuation_score})")

    # Cap quality at 5
    quality = min(quality, 5)
    if quality == 0:
        quality = 1  # minimum

    # Tier label for logging
    tier_map = {5: "S", 4: "A", 3: "B", 2: "C", 1: "D"}
    tier = tier_map.get(quality, "D")
    reasons.append(f"Quality: Tier-{tier} ({quality}/5)")

    # ── Determine trade parameters ──

    if strategy == Strategy.REVERSE:
        # Short pump / Long dump
        if signal.is_pump:
            trade_side = "sell"  # short
            sl_price = current_price * (1 + sl_pct / 100)
            tp_price = current_price * (1 - tp_pct / 100)
        else:
            trade_side = "buy"   # long
            sl_price = current_price * (1 - sl_pct / 100)
            tp_price = current_price * (1 + tp_pct / 100)
    else:
        # Continuation: follow the pump/dump
        if signal.is_pump:
            trade_side = "buy"   # long
            sl_price = current_price * (1 - sl_pct / 100)
            tp_price = current_price * (1 + tp_pct * 2 / 100)  # larger target
        else:
            trade_side = "sell"  # short
            sl_price = current_price * (1 + sl_pct / 100)
            tp_price = current_price * (1 - tp_pct * 2 / 100)

    # Use wick extreme as SL if manipulation detected
    if is_manip and strategy == Strategy.REVERSE:
        if signal.is_pump:
            wick_high = df_1m.tail(3)["high"].max()
            sl_price = wick_high * 1.002  # small buffer above wick
        else:
            wick_low = df_1m.tail(3)["low"].min()
            sl_price = wick_low * 0.998   # small buffer below wick

    return Analysis(
        signal=signal,
        strategy=strategy,
        quality=quality,
        trade_side=trade_side,
        entry_price=current_price,
        sl_price=sl_price,
        tp_price=tp_price,
        rsi=rsi_val,
        near_pivot=near_piv,
        has_reversal_candle=has_rev,
        is_manipulation=is_manip,
        is_sustained_trend=is_sust_trend,
        volume_spike=vol_spike,
        reasons=reasons,
    )
