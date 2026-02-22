"""
Dry-run (paper trading) exchange — simulates Bybit without real API calls.

Uses CoinGecko free API for real market prices, simulates orders locally.
All positions, orders, and P&L are tracked in memory.
"""

import asyncio
import logging
import time
import random
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)


class DryRunExchange:
    """
    Drop-in replacement for Exchange that simulates trading.
    Uses real prices from the live Bybit API (public endpoints, no auth needed)
    but executes orders only in memory.
    """

    def __init__(self, initial_balance: float = 100.0):
        self._balance = initial_balance
        self._initial_balance = initial_balance
        self._positions: dict[str, dict] = {}  # ticker → position
        self._orders: list[dict] = []
        self._trade_history: list[dict] = []
        self._markets_loaded = False
        self._pnl = 0.0

        # Use ccxt for public market data only (no auth)
        import ccxt.async_support as ccxt
        options = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "adjustForTimeDifference": True,
            },
        }
        # Use live Bybit for price data (public, no keys needed)
        self._exchange = ccxt.bybit(options)

    async def _ensure_markets(self):
        if not self._markets_loaded:
            await self._exchange.load_markets()
            self._markets_loaded = True
            logger.info("Markets loaded (dry-run, public data only)")

    async def close(self):
        await self._exchange.close()

    # ── Market data (real prices) ──

    def _symbol(self, ticker: str) -> str:
        return f"{ticker}/USDT:USDT"

    def has_symbol(self, ticker: str) -> bool:
        return self._symbol(ticker) in self._exchange.markets

    async def fetch_ohlcv(
        self, ticker: str, timeframe: str = "1m", limit: int = 200
    ) -> pd.DataFrame:
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        data = await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    async def fetch_ticker_price(self, ticker: str) -> float:
        await self._ensure_markets()
        symbol = self._symbol(ticker)
        t = await self._exchange.fetch_ticker(symbol)
        return float(t["last"])

    async def get_balance(self) -> float:
        return self._balance

    async def get_total_equity(self) -> float:
        # Balance + unrealized PnL
        total = self._balance
        for ticker, pos in self._positions.items():
            try:
                price = await self.fetch_ticker_price(ticker)
                upnl = self._calc_unrealized_pnl(pos, price)
                total += upnl
            except Exception:
                pass
        return total

    def _calc_unrealized_pnl(self, pos: dict, current_price: float) -> float:
        size = pos["size"]
        entry = pos["entry_price"]
        if pos["side"] == "long":
            return size * (current_price - entry)
        else:
            return size * (entry - current_price)

    # ── Position info ──

    async def get_position(self, ticker: str) -> Optional[dict]:
        pos = self._positions.get(ticker)
        if not pos:
            return None
        try:
            price = await self.fetch_ticker_price(ticker)
        except Exception:
            price = pos["entry_price"]

        # Check if TP or SL was hit
        filled = await self._check_tp_sl(ticker, price)
        if filled:
            return None  # Position was closed by TP/SL

        upnl = self._calc_unrealized_pnl(pos, price)
        return {
            "side": pos["side"],
            "size": pos["size"],
            "entry_price": pos["entry_price"],
            "unrealized_pnl": upnl,
            "leverage": pos["leverage"],
            "notional": pos["size"] * price,
        }

    async def _check_tp_sl(self, ticker: str, current_price: float) -> bool:
        """Check if any TP/SL orders for this ticker should be filled."""
        pos = self._positions.get(ticker)
        if not pos:
            return False

        triggered_order = None
        for order in self._orders:
            if order.get("ticker") != ticker:
                continue
            if order["type"] == "tp":
                # TP: for long, price must go UP to TP; for short, DOWN to TP
                if pos["side"] == "long" and current_price >= order["price"]:
                    triggered_order = order
                    break
                elif pos["side"] == "short" and current_price <= order["price"]:
                    triggered_order = order
                    break
            elif order["type"] == "sl":
                # SL: for long, price must DROP to SL; for short, UP to SL
                if pos["side"] == "long" and current_price <= order["price"]:
                    triggered_order = order
                    break
                elif pos["side"] == "short" and current_price >= order["price"]:
                    triggered_order = order
                    break

        if not triggered_order:
            return False

        # Execute the fill
        fill_price = triggered_order["price"]
        pnl = self._calc_unrealized_pnl(pos, fill_price)
        reason = "TP" if triggered_order["type"] == "tp" else "SL"

        self._balance += pos["margin"] + pnl
        self._pnl += pnl
        self._trade_history.append({
            "ticker": ticker,
            "side": pos["side"],
            "entry": pos["entry_price"],
            "exit": fill_price,
            "pnl": pnl,
            "closed_at": time.time(),
            "reason": reason,
        })

        # Clean up
        self._orders = [o for o in self._orders if o.get("ticker") != ticker]
        del self._positions[ticker]

        emoji = "🟩" if pnl >= 0 else "🟥"
        logger.info(
            "%s [DRY] %s hit for %s: entry=%.4f, exit=%.4f, P&L=$%.4f",
            emoji, reason, ticker, pos["entry_price"], fill_price, pnl,
        )
        return True

    async def get_all_positions(self) -> list[dict]:
        result = []
        for ticker, pos in self._positions.items():
            info = await self.get_position(ticker)
            if info:
                info["symbol"] = self._symbol(ticker)
                info["ticker"] = ticker
                result.append(info)
        return result

    # ── Order execution (simulated) ──

    async def set_leverage(self, ticker: str, leverage: int):
        logger.debug("[DRY] Set leverage %dx for %s", leverage, ticker)

    async def set_margin_mode(self, ticker: str, mode: str = "isolated"):
        logger.debug("[DRY] Set margin mode %s for %s", mode, ticker)

    async def open_market_order(
        self, ticker: str, side: str, amount_usdt: float, leverage: int,
    ) -> dict:
        await self._ensure_markets()
        price = await self.fetch_ticker_price(ticker)

        notional = amount_usdt * leverage
        size = notional / price

        # Round to exchange precision
        symbol = self._symbol(ticker)
        market = self._exchange.market(symbol)
        size = float(self._exchange.amount_to_precision(symbol, size))

        pos_side = "long" if side == "buy" else "short"

        self._positions[ticker] = {
            "side": pos_side,
            "size": size,
            "entry_price": price,
            "leverage": leverage,
            "margin": amount_usdt,
            "opened_at": time.time(),
        }
        self._balance -= amount_usdt

        logger.info(
            "[DRY] Opened %s %s: size=%.4f, price=%.4f, margin=$%.2f, lev=%dx",
            side.upper(), ticker, size, price, amount_usdt, leverage,
        )

        return {
            "id": f"dry_{int(time.time())}_{random.randint(1000,9999)}",
            "symbol": symbol,
            "side": side,
            "amount": size,
            "average": price,
            "price": price,
            "status": "closed",
        }

    async def close_position(self, ticker: str) -> Optional[dict]:
        pos = self._positions.get(ticker)
        if not pos:
            return None

        price = await self.fetch_ticker_price(ticker)
        pnl = self._calc_unrealized_pnl(pos, price)

        self._balance += pos["margin"] + pnl
        self._pnl += pnl

        self._trade_history.append({
            "ticker": ticker,
            "side": pos["side"],
            "entry": pos["entry_price"],
            "exit": price,
            "pnl": pnl,
            "closed_at": time.time(),
        })

        del self._positions[ticker]

        logger.info(
            "[DRY] Closed %s %s: entry=%.4f, exit=%.4f, P&L=$%.2f",
            ticker, pos["side"], pos["entry_price"], price, pnl,
        )
        return {"id": f"dry_close_{int(time.time())}"}

    async def place_limit_tp(
        self, ticker: str, side: str, amount: float, price: float
    ) -> dict:
        order = {
            "id": f"dry_tp_{int(time.time())}",
            "type": "tp",
            "ticker": ticker,
            "side": side,
            "amount": amount,
            "price": price,
        }
        self._orders.append(order)
        logger.info("[DRY] TP order: %s %s @ %.4f", side, ticker, price)
        return order

    async def place_stop_market(
        self, ticker: str, side: str, amount: float, stop_price: float
    ) -> dict:
        order = {
            "id": f"dry_sl_{int(time.time())}",
            "type": "sl",
            "ticker": ticker,
            "side": side,
            "amount": amount,
            "price": stop_price,
        }
        self._orders.append(order)
        logger.info("[DRY] SL order: %s %s trigger=%.4f", side, ticker, stop_price)
        return order

    async def cancel_all_orders(self, ticker: str):
        self._orders = [o for o in self._orders if o.get("ticker") != ticker]
        logger.debug("[DRY] Cancelled all orders for %s", ticker)

    async def place_limit_order(
        self, ticker: str, side: str, amount_usdt: float, price: float, leverage: int
    ) -> dict:
        order = {
            "id": f"dry_avg_{int(time.time())}",
            "type": "avg",
            "ticker": ticker,
            "side": side,
            "amount_usdt": amount_usdt,
            "price": price,
            "leverage": leverage,
        }
        self._orders.append(order)
        logger.info("[DRY] Limit order: %s %s @ %.4f, margin=$%.2f", side, ticker, price, amount_usdt)
        return order

    def get_session_summary(self) -> str:
        """Return a summary of dry-run session."""
        lines = [
            f"Balance: ${self._balance:.2f} (started: ${self._initial_balance:.2f})",
            f"Total P&L: ${self._pnl:.2f}",
            f"Trades: {len(self._trade_history)}",
            f"Open positions: {len(self._positions)}",
        ]
        for h in self._trade_history:
            emoji = "+" if h["pnl"] >= 0 else ""
            lines.append(f"  {h['ticker']} {h['side']}: {emoji}${h['pnl']:.2f}")
        return "\n".join(lines)
