"""
Risk manager — enforces all risk management rules per phase.
"""

import logging
import time
from dataclasses import dataclass, field

from config import PhaseConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class DailyStats:
    """Track daily P&L and trade counts."""
    date: str = ""
    total_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0
    stopped_out: bool = False

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.win_count / self.trade_count * 100


class RiskManager:
    """Enforces risk rules: margin sizing, stop limits, daily limits."""

    def __init__(self):
        self._daily_stats = DailyStats()
        self._last_trade_time: dict[str, float] = {}  # ticker → timestamp
        self._reentry_count: dict[str, int] = {}       # ticker → count today
        self._current_date = ""

    def _reset_daily_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._daily_stats.trade_count > 0:
                logger.info(
                    "Day %s summary: P&L=$%.2f, trades=%d, WR=%.1f%%",
                    self._current_date,
                    self._daily_stats.total_pnl,
                    self._daily_stats.trade_count,
                    self._daily_stats.win_rate,
                )
            self._daily_stats = DailyStats(date=today)
            self._reentry_count.clear()
            self._current_date = today

    def get_daily_stats(self) -> DailyStats:
        self._reset_daily_if_needed()
        return self._daily_stats

    def can_trade(self, ticker: str, balance: float) -> tuple[bool, str]:
        """
        Check if we're allowed to open a new trade.
        Returns (allowed, reason).
        """
        self._reset_daily_if_needed()
        cfg = get_config(balance)

        # Daily stop reached?
        max_daily_loss = balance * cfg.daily_stop_pct / 100
        if self._daily_stats.total_pnl <= -max_daily_loss:
            self._daily_stats.stopped_out = True
            return False, f"Daily stop reached (${self._daily_stats.total_pnl:.2f})"

        # Cooldown per ticker
        from config import SIGNAL_COOLDOWN_SEC
        last_t = self._last_trade_time.get(ticker, 0)
        if time.time() - last_t < SIGNAL_COOLDOWN_SEC:
            return False, f"Cooldown active for {ticker}"

        # Max 1 re-entry per ticker per day
        reentries = self._reentry_count.get(ticker, 0)
        if reentries >= 2:
            return False, f"Max re-entries reached for {ticker} today"

        return True, "OK"

    def calc_margin(self, balance: float) -> float:
        """Calculate the margin (USDT) to use for a single trade."""
        cfg = get_config(balance)
        return balance * cfg.margin_pct / 100

    def calc_avg_margin(self, balance: float) -> float:
        """Calculate margin per averaging entry."""
        cfg = get_config(balance)
        return balance * cfg.avg_margin_pct / 100

    def get_leverage(self, balance: float) -> int:
        """Get the leverage for the current phase."""
        return get_config(balance).leverage

    def get_phase_config(self, balance: float) -> PhaseConfig:
        return get_config(balance)

    def check_max_loss(self, balance: float, unrealized_pnl: float) -> bool:
        """Check if unrealized loss exceeds max stop per trade."""
        cfg = get_config(balance)
        max_loss = balance * cfg.max_stop_pct / 100
        return unrealized_pnl <= -max_loss

    def record_trade(self, ticker: str, pnl: float):
        """Record a completed trade."""
        self._reset_daily_if_needed()
        self._daily_stats.trade_count += 1
        self._daily_stats.total_pnl += pnl
        if pnl >= 0:
            self._daily_stats.win_count += 1
        else:
            self._daily_stats.loss_count += 1

        self._last_trade_time[ticker] = time.time()
        self._reentry_count[ticker] = self._reentry_count.get(ticker, 0) + 1

        logger.info(
            "Trade recorded: %s P&L=$%.2f | Day: $%.2f (%d/%d) WR=%.1f%%",
            ticker, pnl,
            self._daily_stats.total_pnl,
            self._daily_stats.win_count,
            self._daily_stats.trade_count,
            self._daily_stats.win_rate,
        )
