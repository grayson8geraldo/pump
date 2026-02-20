"""
Pump & Dump Trading Bot — main entry point.

Connects to:
  1. Telegram screener channel (reads signals)
  2. Bybit exchange (executes trades)

Strategies:
  - Reverse Scalping (80%): short pumps, long dumps
  - Continuation (20%): follow true pumps/dumps
  - Averaging: grid-like entries for higher win rate

Growth phases:
  Phase 1: $100 → $1,000 (aggressive)
  Phase 2: $1,000 → $10,000 (moderate)
  Phase 3: $10,000 → $100,000 (conservative)
"""

import asyncio
import logging
import sys
import time
from collections import deque

import config
from config import get_config, get_phase_for_balance
from exchange import Exchange
from signal_parser import Signal, SignalDirection, is_spam_wave
from analyzer import analyze_signal, Strategy
from risk_manager import RiskManager
from position_manager import PositionManager
from trade_logger import TradeLogger
from telegram_listener import TelegramListener

# ── Logging setup ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("bot")


class TradingBot:
    """Main bot orchestrator."""

    def __init__(self):
        self._exchange = Exchange()
        self._risk = RiskManager()
        self._positions = PositionManager(self._exchange, self._risk)
        self._trade_logger = TradeLogger()
        self._recent_signals: deque[Signal] = deque(maxlen=100)
        self._running = False

    async def start(self):
        """Initialize and start the bot."""
        logger.info("=" * 60)
        logger.info("  PUMP & DUMP TRADING BOT")
        logger.info("=" * 60)

        # Init trade logger
        await self._trade_logger.init()

        # Check balance and determine phase
        balance = await self._exchange.get_total_equity()
        phase = get_phase_for_balance(balance)
        cfg = get_config(balance)

        logger.info("Balance: $%.2f", balance)
        logger.info("Phase: %s", cfg.name)
        logger.info("Leverage: %dx | Margin: %.1f%% | SL: %.1f%% | TP: %.1f%%",
                     cfg.leverage, cfg.margin_pct, cfg.sl_price_pct, cfg.tp_price_pct)
        logger.info("Averaging: %s", "ON" if cfg.use_averaging else "OFF")
        logger.info("Testnet: %s", config.BYBIT_TESTNET)
        logger.info("Channels: %s", ", ".join(config.SIGNAL_CHANNELS))
        logger.info("=" * 60)

        # Start position monitor in background
        asyncio.create_task(
            self._positions.start_monitoring(self._exchange.get_total_equity)
        )

        # Start Telegram listener
        self._running = True
        listener = TelegramListener(on_signal=self._handle_signal)

        try:
            await listener.start()
        except KeyboardInterrupt:
            logger.info("Shutting down…")
        finally:
            await self._shutdown(listener)

    async def _shutdown(self, listener: TelegramListener):
        """Graceful shutdown."""
        self._running = False
        await listener.stop()

        # Log final stats
        stats = await self._trade_logger.get_total_stats()
        logger.info("=" * 60)
        logger.info("  SESSION SUMMARY")
        logger.info("  Trades: %d | P&L: $%.2f | WR: %.1f%%",
                     stats["total_trades"], stats["total_pnl"], stats["win_rate"])
        logger.info("=" * 60)

        await self._trade_logger.close()
        await self._exchange.close()

    async def _handle_signal(self, signal: Signal):
        """
        Main signal handler — the core decision pipeline:

        1. Filter blacklisted coins
        2. Detect BTC spam waves
        3. Check risk limits
        4. Fetch chart data
        5. Analyze setup quality
        6. Execute trade if quality meets threshold
        """
        ticker = signal.ticker

        # ── 1. Blacklist check ──
        if ticker in config.BLACKLISTED_COINS:
            logger.info("Skipping blacklisted coin: %s", ticker)
            await self._log_signal(signal, "BLACKLISTED")
            return

        # ── 2. Check if symbol exists on Bybit ──
        await self._exchange._ensure_markets()
        if not self._exchange.has_symbol(ticker):
            logger.debug("Symbol %s not on Bybit, skipping", ticker)
            await self._log_signal(signal, "NOT_ON_EXCHANGE")
            return

        # ── 3. BTC spam wave detection ──
        self._recent_signals.append(signal)
        if is_spam_wave(
            list(self._recent_signals),
            config.BTC_SPAM_WINDOW_SEC,
            config.BTC_SPAM_THRESHOLD,
        ):
            logger.warning("BTC spam wave detected, skipping %s", ticker)
            await self._log_signal(signal, "SPAM_WAVE")
            return

        # ── 4. Already in position? ──
        if self._positions.has_position(ticker):
            logger.info("Already in position for %s, skipping", ticker)
            await self._log_signal(signal, "ALREADY_IN_POSITION")
            return

        # ── 5. Risk checks ──
        balance = await self._exchange.get_total_equity()
        can_trade, reason = self._risk.can_trade(ticker, balance)
        if not can_trade:
            logger.info("Risk check failed for %s: %s", ticker, reason)
            await self._log_signal(signal, f"RISK_BLOCKED: {reason}")
            return

        # ── 6. Max concurrent trades ──
        cfg = get_config(balance)
        if self._positions.open_count >= cfg.max_concurrent_trades:
            # Check if new signal is opposite direction of existing position
            # If so, this could be a hedging opportunity — but we skip for safety
            logger.info(
                "Max positions reached (%d/%d), skipping %s",
                self._positions.open_count, cfg.max_concurrent_trades, ticker,
            )
            await self._log_signal(signal, "MAX_POSITIONS")
            return

        # ── 7. Fetch chart data ──
        try:
            df_1m = await self._exchange.fetch_ohlcv(ticker, "1m", 200)
            df_1h = await self._exchange.fetch_ohlcv(ticker, "1h", 200)
            current_price = await self._exchange.fetch_ticker_price(ticker)
        except Exception:
            logger.exception("Failed to fetch data for %s", ticker)
            await self._log_signal(signal, "DATA_ERROR")
            return

        # ── 8. Analyze ──
        analysis = analyze_signal(
            signal=signal,
            df_1m=df_1m,
            df_1h=df_1h,
            current_price=current_price,
            sl_pct=cfg.sl_price_pct,
            tp_pct=cfg.tp_price_pct,
        )

        logger.info(
            "Analysis for %s: strategy=%s, quality=%d/5, side=%s",
            ticker, analysis.strategy.value, analysis.quality, analysis.trade_side,
        )
        for r in analysis.reasons:
            logger.info("  • %s", r)

        # ── 9. Quality filter ──
        if analysis.quality < config.MIN_SETUP_QUALITY:
            logger.info(
                "Quality too low for %s (%d < %d), skipping",
                ticker, analysis.quality, config.MIN_SETUP_QUALITY,
            )
            await self._log_signal(signal, f"LOW_QUALITY ({analysis.quality})")
            return

        # ── 10. Skip if continuation detected but no strong confirmation ──
        if analysis.strategy == Strategy.CONTINUATION and analysis.quality < 3:
            logger.info(
                "Continuation signal for %s but quality=%d, skipping (need ≥3)",
                ticker, analysis.quality,
            )
            await self._log_signal(signal, "CONTINUATION_LOW_QUALITY")
            return

        # ── 11. Execute trade ──
        logger.info(
            "🚀 Opening trade: %s %s %s (quality=%d)",
            analysis.trade_side.upper(), ticker, analysis.strategy.value,
            analysis.quality,
        )

        success = await self._positions.open_trade(analysis, balance)
        action = "TRADED" if success else "TRADE_FAILED"
        await self._log_signal(signal, action)

        if success:
            # Log phase progress
            new_balance = await self._exchange.get_total_equity()
            phase = get_phase_for_balance(new_balance)
            new_cfg = get_config(new_balance)
            logger.info(
                "Phase: %s | Balance: $%.2f / $%.2f target",
                new_cfg.name, new_balance, new_cfg.target_balance,
            )

    async def _log_signal(self, signal: Signal, action: str):
        """Log signal to database."""
        try:
            await self._trade_logger.log_signal(
                ticker=signal.ticker,
                direction=signal.direction.value,
                change_pct=signal.change_pct,
                bell_count=signal.bell_count,
                exchanges=signal.exchanges,
                action=action,
                raw_text=signal.raw_text,
            )
        except Exception:
            logger.debug("Failed to log signal to DB")


async def main():
    bot = TradingBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
