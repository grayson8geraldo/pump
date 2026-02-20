"""
Bybit exchange wrapper built on ccxt.
Handles all exchange communication: market data, order placement, position management.
"""

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

import config

logger = logging.getLogger(__name__)


class Exchange:
    """Async wrapper around Bybit futures via ccxt."""

    def __init__(self):
        options = {
            "apiKey": config.BYBIT_API_KEY,
            "secret": config.BYBIT_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",      # USDT perpetual futures
                "adjustForTimeDifference": True,
            },
        }
        if config.BYBIT_TESTNET:
            options["sandbox"] = True

        self._exchange = ccxt.bybit(options)
        self._markets_loaded = False

    async def _ensure_markets(self):
        if not self._markets_loaded:
            await self._exchange.load_markets()
            self._markets_loaded = True

    async def close(self):
        await self._exchange.close()

    # ── Market data ──

    def _symbol(self, ticker: str) -> str:
        """Convert ticker to ccxt symbol, e.g. 'BTC' → 'BTC/USDT:USDT'."""
        return f"{ticker}/USDT:USDT"

    def has_symbol(self, ticker: str) -> bool:
        """Check if the symbol exists on the exchange."""
        return self._symbol(ticker) in self._exchange.markets

    async def fetch_ohlcv(
        self, ticker: str, timeframe: str = "1m", limit: int = 200
    ) -> pd.DataFrame:
        """Fetch OHLCV candles and return as a DataFrame."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        data = await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    async def fetch_ticker_price(self, ticker: str) -> float:
        """Get the latest price."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        t = await self._exchange.fetch_ticker(symbol)
        return float(t["last"])

    async def get_balance(self) -> float:
        """Get USDT balance available for trading."""
        await self._ensure_markets()
        balance = await self._exchange.fetch_balance({"type": "swap"})
        usdt = balance.get("USDT", {})
        return float(usdt.get("free", 0.0))

    async def get_total_equity(self) -> float:
        """Get total account equity in USDT."""
        await self._ensure_markets()
        balance = await self._exchange.fetch_balance({"type": "swap"})
        usdt = balance.get("USDT", {})
        return float(usdt.get("total", 0.0))

    # ── Position info ──

    async def get_position(self, ticker: str) -> Optional[dict]:
        """
        Get current position for a ticker.
        Returns dict with keys: side, size, entry_price, unrealized_pnl, leverage
        or None if no position.
        """
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        positions = await self._exchange.fetch_positions([symbol])
        for pos in positions:
            size = float(pos.get("contracts", 0))
            if size > 0:
                return {
                    "side": pos["side"],  # 'long' or 'short'
                    "size": size,
                    "entry_price": float(pos["entryPrice"]),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": int(pos.get("leverage", 1)),
                    "notional": float(pos.get("notional", 0)),
                }
        return None

    async def get_all_positions(self) -> list[dict]:
        """Get all open positions."""
        await self._ensure_markets()
        positions = await self._exchange.fetch_positions()
        result = []
        for pos in positions:
            size = float(pos.get("contracts", 0))
            if size > 0:
                result.append({
                    "symbol": pos["symbol"],
                    "ticker": pos["symbol"].split("/")[0],
                    "side": pos["side"],
                    "size": size,
                    "entry_price": float(pos["entryPrice"]),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": int(pos.get("leverage", 1)),
                    "notional": float(pos.get("notional", 0)),
                })
        return result

    # ── Order execution ──

    async def set_leverage(self, ticker: str, leverage: int):
        """Set leverage for a symbol."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        try:
            await self._exchange.set_leverage(leverage, symbol)
        except ccxt.ExchangeError as e:
            # Some exchanges error if leverage is already set
            logger.debug("set_leverage note: %s", e)

    async def set_margin_mode(self, ticker: str, mode: str = "isolated"):
        """Set margin mode: 'isolated' or 'cross'."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        try:
            await self._exchange.set_margin_mode(mode, symbol)
        except ccxt.ExchangeError as e:
            logger.debug("set_margin_mode note: %s", e)

    async def open_market_order(
        self,
        ticker: str,
        side: str,
        amount_usdt: float,
        leverage: int,
    ) -> dict:
        """
        Open a market order.
        side: 'buy' (long) or 'sell' (short)
        amount_usdt: margin in USDT (will be multiplied by leverage)
        Returns order info dict.
        """
        await self._ensure_markets()
        symbol = self._symbol(ticker)

        await self.set_leverage(ticker, leverage)
        await self.set_margin_mode(ticker, "isolated")

        price = await self.fetch_ticker_price(ticker)
        # Calculate contract amount
        notional = amount_usdt * leverage
        amount = notional / price

        # Round to exchange precision
        market = self._exchange.market(symbol)
        amount = self._exchange.amount_to_precision(symbol, amount)

        order = await self._exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=float(amount),
        )

        logger.info(
            "Opened %s %s: amount=%.4f, price=%.4f, margin=$%.2f, lev=%dx",
            side.upper(), ticker, float(amount), price, amount_usdt, leverage,
        )
        return order

    async def close_position(self, ticker: str) -> Optional[dict]:
        """Close the entire position for a ticker."""
        await self._ensure_markets()
        pos = await self.get_position(ticker)
        if not pos:
            return None

        symbol = self._symbol(ticker)
        close_side = "sell" if pos["side"] == "long" else "buy"
        order = await self._exchange.create_order(
            symbol=symbol,
            type="market",
            side=close_side,
            amount=pos["size"],
            params={"reduceOnly": True},
        )
        logger.info("Closed position %s %s", ticker, pos["side"])
        return order

    async def place_limit_tp(
        self, ticker: str, side: str, amount: float, price: float
    ) -> dict:
        """Place a limit take-profit order (reduce only)."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        price = float(self._exchange.price_to_precision(symbol, price))
        amount = float(self._exchange.amount_to_precision(symbol, amount))

        order = await self._exchange.create_order(
            symbol=symbol,
            type="limit",
            side=side,
            amount=amount,
            price=price,
            params={"reduceOnly": True},
        )
        logger.info("TP order: %s %s @ %.4f, qty=%.4f", side, ticker, price, amount)
        return order

    async def place_stop_market(
        self, ticker: str, side: str, amount: float, stop_price: float
    ) -> dict:
        """Place a stop-market order (stop-loss)."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        stop_price = float(self._exchange.price_to_precision(symbol, stop_price))
        amount = float(self._exchange.amount_to_precision(symbol, amount))

        trigger_direction = "above" if side == "buy" else "below"
        order = await self._exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
            params={
                "reduceOnly": True,
                "stopLoss": {
                    "triggerPrice": stop_price,
                    "type": "market",
                },
            },
        )
        logger.info("SL order: %s %s trigger=%.4f, qty=%.4f", side, ticker, stop_price, amount)
        return order

    async def cancel_all_orders(self, ticker: str):
        """Cancel all open orders for a ticker."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        try:
            await self._exchange.cancel_all_orders(symbol)
            logger.info("Cancelled all orders for %s", ticker)
        except ccxt.ExchangeError as e:
            logger.debug("cancel_all_orders note: %s", e)

    async def place_limit_order(
        self, ticker: str, side: str, amount_usdt: float, price: float, leverage: int
    ) -> dict:
        """Place a limit order for averaging entry."""
        await self._ensure_markets()
        symbol = self._symbol(ticker)

        notional = amount_usdt * leverage
        amount = notional / price
        amount = float(self._exchange.amount_to_precision(symbol, amount))
        price = float(self._exchange.price_to_precision(symbol, price))

        order = await self._exchange.create_order(
            symbol=symbol,
            type="limit",
            side=side,
            amount=amount,
            price=price,
        )
        logger.info(
            "Limit order: %s %s @ %.4f, qty=%.4f, margin=$%.2f",
            side, ticker, price, amount, amount_usdt,
        )
        return order
