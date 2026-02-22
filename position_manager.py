"""
Position manager — handles open trades, averaging, TP/SL placement,
and position monitoring.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from exchange import Exchange
from risk_manager import RiskManager
from analyzer import Analysis, Strategy
from config import get_config

logger = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    """Tracks a position opened by the bot."""
    ticker: str
    side: str                # 'buy' or 'sell'
    strategy: Strategy
    entry_price: float
    sl_price: float
    tp_price: float
    margin_used: float
    leverage: int
    avg_count: int = 0       # number of averaging entries placed
    opened_at: float = 0.0
    quality: int = 1
    analysis_reasons: list[str] = field(default_factory=list)


class PositionManager:
    """Manages all open bot positions."""

    def __init__(self, exchange: Exchange, risk_manager: RiskManager):
        self._exchange = exchange
        self._risk = risk_manager
        self._positions: dict[str, ManagedPosition] = {}  # ticker → position
        self._monitor_task: asyncio.Task | None = None

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions

    def get_position(self, ticker: str) -> ManagedPosition | None:
        return self._positions.get(ticker)

    async def open_trade(self, analysis: Analysis, balance: float) -> bool:
        """
        Open a new trade based on the analysis.
        Returns True if the trade was opened successfully.
        """
        ticker = analysis.signal.ticker
        cfg = get_config(balance)

        # Determine if we use averaging strategy
        use_avg = cfg.use_averaging and analysis.strategy == Strategy.REVERSE

        if use_avg:
            margin = self._risk.calc_avg_margin(balance)
        else:
            margin = self._risk.calc_margin(balance)

        leverage = cfg.leverage

        try:
            # Open market order
            order = await self._exchange.open_market_order(
                ticker=ticker,
                side=analysis.trade_side,
                amount_usdt=margin,
                leverage=leverage,
            )

            # Get actual fill price
            fill_price = float(order.get("average", analysis.entry_price))

            # Recalculate SL/TP based on fill
            if analysis.trade_side == "sell":
                sl_price = fill_price * (1 + cfg.sl_price_pct / 100)
                if analysis.strategy == Strategy.REVERSE:
                    tp_price = fill_price * (1 - cfg.tp_price_pct / 100)
                else:
                    tp_price = fill_price * (1 - cfg.tp_price_pct * 2 / 100)
            else:
                sl_price = fill_price * (1 - cfg.sl_price_pct / 100)
                if analysis.strategy == Strategy.REVERSE:
                    tp_price = fill_price * (1 + cfg.tp_price_pct / 100)
                else:
                    tp_price = fill_price * (1 + cfg.tp_price_pct * 2 / 100)

            # Use wick-based SL from analysis if available
            if analysis.is_manipulation and analysis.strategy == Strategy.REVERSE:
                sl_price = analysis.sl_price

            # Record position
            pos = ManagedPosition(
                ticker=ticker,
                side=analysis.trade_side,
                strategy=analysis.strategy,
                entry_price=fill_price,
                sl_price=sl_price,
                tp_price=tp_price,
                margin_used=margin,
                leverage=leverage,
                opened_at=time.time(),
                quality=analysis.quality,
                analysis_reasons=analysis.reasons,
            )
            self._positions[ticker] = pos

            # Place TP and SL orders
            ex_pos = await self._exchange.get_position(ticker)
            if ex_pos:
                close_side = "buy" if analysis.trade_side == "sell" else "sell"

                if use_avg:
                    # For averaging: use ROI-based TP and proportional SL
                    roi_tp = cfg.avg_tp_roi_pct / 100
                    roi_sl = roi_tp * 1.5  # SL = 1.5x TP (18% ROI vs 12% ROI)
                    if analysis.trade_side == "sell":
                        tp_price = fill_price * (1 - roi_tp / leverage)
                        sl_price = fill_price * (1 + roi_sl / leverage)
                    else:
                        tp_price = fill_price * (1 + roi_tp / leverage)
                        sl_price = fill_price * (1 - roi_sl / leverage)
                    pos.tp_price = tp_price
                    pos.sl_price = sl_price

                await self._exchange.place_limit_tp(
                    ticker, close_side, ex_pos["size"], tp_price
                )

                # Place SL
                await self._exchange.place_stop_market(
                    ticker, close_side, ex_pos["size"], sl_price
                )

                # Place first averaging limit order
                if use_avg and cfg.avg_max_entries > 0:
                    await self._place_averaging_order(pos, balance, 1)

            logger.info(
                "✅ Opened %s %s: entry=%.4f, SL=%.4f, TP=%.4f, margin=$%.2f, lev=%dx | %s",
                "LONG" if analysis.trade_side == "buy" else "SHORT",
                ticker, fill_price, sl_price, tp_price, margin, leverage,
                analysis.strategy.value,
            )
            return True

        except Exception:
            logger.exception("Failed to open trade for %s", ticker)
            return False

    async def _place_averaging_order(
        self, pos: ManagedPosition, balance: float, entry_num: int
    ):
        """Place an averaging limit order at the next level."""
        cfg = get_config(balance)
        if entry_num > cfg.avg_max_entries:
            return

        avg_margin = self._risk.calc_avg_margin(balance)
        step = cfg.avg_step_pct / 100

        if pos.side == "sell":
            # Shorting: average up
            avg_price = pos.entry_price * (1 + step * entry_num)
            order_side = "sell"
        else:
            # Longing: average down
            avg_price = pos.entry_price * (1 - step * entry_num)
            order_side = "buy"

        try:
            await self._exchange.place_limit_order(
                ticker=pos.ticker,
                side=order_side,
                amount_usdt=avg_margin,
                price=avg_price,
                leverage=cfg.leverage,
            )
            pos.avg_count = entry_num
            logger.info(
                "Averaging order #%d for %s @ %.4f ($%.2f)",
                entry_num, pos.ticker, avg_price, avg_margin,
            )
        except Exception:
            logger.exception(
                "Failed to place averaging order #%d for %s",
                entry_num, pos.ticker,
            )

    async def close_trade(self, ticker: str, reason: str = "") -> float:
        """
        Close a position and return the realized P&L.
        """
        pos = self._positions.get(ticker)
        if not pos:
            return 0.0

        # Get unrealized P&L before closing
        ex_pos = await self._exchange.get_position(ticker)
        pnl = ex_pos["unrealized_pnl"] if ex_pos else 0.0

        # Cancel all orders
        await self._exchange.cancel_all_orders(ticker)

        # Close position
        await self._exchange.close_position(ticker)

        # Record
        self._risk.record_trade(ticker, pnl)
        del self._positions[ticker]

        logger.info(
            "❎ Closed %s: P&L=$%.2f (%s)",
            ticker, pnl, reason,
        )
        return pnl

    async def monitor_positions(self, balance: float):
        """
        Check all open positions for:
        - Max stop exceeded → force close
        - Averaging trigger
        - Move SL to breakeven after profit
        """
        cfg = get_config(balance)
        tickers_to_close = []

        for ticker, pos in list(self._positions.items()):
            try:
                ex_pos = await self._exchange.get_position(ticker)
                if not ex_pos:
                    # Position was closed externally (TP/SL hit)
                    logger.info("Position %s closed externally", ticker)
                    self._risk.record_trade(ticker, 0.0)  # P&L unknown
                    tickers_to_close.append(ticker)
                    continue

                unrealized = ex_pos["unrealized_pnl"]

                # Check max stop per trade
                if self._risk.check_max_loss(balance, unrealized):
                    logger.warning(
                        "⚠️ Max stop triggered for %s (P&L=$%.2f)",
                        ticker, unrealized,
                    )
                    tickers_to_close.append(ticker)
                    continue

                # Check for averaging — if position size grew, update TP
                if cfg.use_averaging and pos.strategy == Strategy.REVERSE:
                    # Check if we need to place next averaging order
                    expected_entries = pos.avg_count + 1
                    if expected_entries <= cfg.avg_max_entries:
                        # Check if current price has moved enough
                        price = await self._exchange.fetch_ticker_price(ticker)
                        step = cfg.avg_step_pct / 100

                        if pos.side == "sell":
                            trigger = pos.entry_price * (1 + step * expected_entries)
                            if price >= trigger:
                                await self._place_averaging_order(pos, balance, expected_entries)
                        else:
                            trigger = pos.entry_price * (1 - step * expected_entries)
                            if price <= trigger:
                                await self._place_averaging_order(pos, balance, expected_entries)

                # Move SL to breakeven if in profit
                roi = unrealized / pos.margin_used * 100 if pos.margin_used else 0
                if roi >= 10:  # 10% ROI
                    # Move SL to entry
                    close_side = "buy" if pos.side == "sell" else "sell"
                    try:
                        await self._exchange.cancel_all_orders(ticker)
                        await self._exchange.place_stop_market(
                            ticker, close_side, ex_pos["size"], pos.entry_price
                        )
                        await self._exchange.place_limit_tp(
                            ticker, close_side, ex_pos["size"], pos.tp_price
                        )
                        logger.info("Moved SL to breakeven for %s", ticker)
                    except Exception:
                        logger.debug("Could not move SL for %s", ticker)

            except Exception:
                logger.exception("Error monitoring %s", ticker)

        # Close positions that need closing
        for ticker in tickers_to_close:
            if ticker in self._positions:
                await self.close_trade(ticker, reason="max stop / external close")

    async def start_monitoring(self, get_balance):
        """Background task to periodically check positions."""
        while True:
            try:
                if self._positions:
                    balance = await get_balance()
                    await self.monitor_positions(balance)
            except Exception:
                logger.exception("Monitor loop error")
            await asyncio.sleep(10)

    def get_all_positions_info(self) -> list[dict]:
        """Return summary of all managed positions."""
        result = []
        for ticker, pos in self._positions.items():
            result.append({
                "ticker": ticker,
                "side": "LONG" if pos.side == "buy" else "SHORT",
                "strategy": pos.strategy.value,
                "entry": pos.entry_price,
                "sl": pos.sl_price,
                "tp": pos.tp_price,
                "margin": pos.margin_used,
                "leverage": pos.leverage,
                "quality": pos.quality,
                "avg_count": pos.avg_count,
                "age_min": (time.time() - pos.opened_at) / 60,
            })
        return result
