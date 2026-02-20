"""
Trade logger — persists all trades and statistics to SQLite.
"""

import asyncio
import logging
import time

import aiosqlite

import config

logger = logging.getLogger(__name__)


class TradeLogger:
    """Async SQLite logger for trade history and stats."""

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or config.DB_FILE
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                date TEXT,
                ticker TEXT,
                side TEXT,
                strategy TEXT,
                entry_price REAL,
                exit_price REAL,
                sl_price REAL,
                tp_price REAL,
                margin REAL,
                leverage INTEGER,
                pnl REAL,
                quality INTEGER,
                reasons TEXT,
                duration_sec REAL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                start_balance REAL,
                end_balance REAL,
                total_pnl REAL,
                trade_count INTEGER,
                win_count INTEGER,
                loss_count INTEGER,
                phase TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                ticker TEXT,
                direction TEXT,
                change_pct REAL,
                bell_count INTEGER,
                exchanges TEXT,
                action_taken TEXT,
                raw_text TEXT
            );
        """)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def log_signal(
        self,
        ticker: str,
        direction: str,
        change_pct: float,
        bell_count: int,
        exchanges: list[str],
        action: str,
        raw_text: str = "",
    ):
        """Log every signal received, whether traded or not."""
        await self._db.execute(
            """INSERT INTO signals
               (timestamp, ticker, direction, change_pct, bell_count, exchanges, action_taken, raw_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                ticker,
                direction,
                change_pct,
                bell_count,
                ",".join(exchanges),
                action,
                raw_text[:500],
            ),
        )
        await self._db.commit()

    async def log_trade(
        self,
        ticker: str,
        side: str,
        strategy: str,
        entry_price: float,
        exit_price: float,
        sl_price: float,
        tp_price: float,
        margin: float,
        leverage: int,
        pnl: float,
        quality: int,
        reasons: list[str],
        duration_sec: float,
    ):
        """Log a completed trade."""
        today = time.strftime("%Y-%m-%d")
        await self._db.execute(
            """INSERT INTO trades
               (timestamp, date, ticker, side, strategy, entry_price, exit_price,
                sl_price, tp_price, margin, leverage, pnl, quality, reasons, duration_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                today,
                ticker,
                side,
                strategy,
                entry_price,
                exit_price,
                sl_price,
                tp_price,
                margin,
                leverage,
                pnl,
                quality,
                "; ".join(reasons),
                duration_sec,
            ),
        )
        await self._db.commit()

    async def update_daily_stats(
        self,
        start_balance: float,
        end_balance: float,
        total_pnl: float,
        trade_count: int,
        win_count: int,
        loss_count: int,
        phase: str,
    ):
        """Update or insert daily stats."""
        today = time.strftime("%Y-%m-%d")
        await self._db.execute(
            """INSERT OR REPLACE INTO daily_stats
               (date, start_balance, end_balance, total_pnl, trade_count,
                win_count, loss_count, phase)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, start_balance, end_balance, total_pnl,
             trade_count, win_count, loss_count, phase),
        )
        await self._db.commit()

    async def get_total_stats(self) -> dict:
        """Get lifetime statistics."""
        async with self._db.execute(
            "SELECT COUNT(*), SUM(pnl), AVG(pnl) FROM trades"
        ) as cursor:
            row = await cursor.fetchone()
            total_trades = row[0] or 0
            total_pnl = row[1] or 0.0
            avg_pnl = row[2] or 0.0

        async with self._db.execute(
            "SELECT COUNT(*) FROM trades WHERE pnl >= 0"
        ) as cursor:
            wins = (await cursor.fetchone())[0] or 0

        async with self._db.execute(
            "SELECT MAX(pnl), MIN(pnl) FROM trades"
        ) as cursor:
            row = await cursor.fetchone()
            best = row[0] or 0.0
            worst = row[1] or 0.0

        return {
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "win_rate": (wins / total_trades * 100) if total_trades else 0,
            "best_trade": best,
            "worst_trade": worst,
        }

    async def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Get the most recent trades."""
        async with self._db.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
