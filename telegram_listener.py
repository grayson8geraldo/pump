"""
Telegram listener — connects to the screener channel and forwards
parsed signals to the trading engine.
"""

import asyncio
import logging
from typing import Callable, Awaitable

from telethon import TelegramClient, events

import config
from signal_parser import Signal, parse_signal, is_btc_trend_signal

logger = logging.getLogger(__name__)

SignalCallback = Callable[[Signal], Awaitable[None]]


class TelegramListener:
    """Listens to Telegram channels for screener signals."""

    def __init__(self, on_signal: SignalCallback):
        self._on_signal = on_signal
        self._client: TelegramClient | None = None

    async def start(self):
        """Connect to Telegram and start listening."""
        if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
            raise RuntimeError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env"
            )

        self._client = TelegramClient(
            "pump_bot_session",
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )

        await self._client.start(phone=config.TELEGRAM_PHONE)
        logger.info("Telegram client connected")

        # Resolve channel entities
        channels = []
        for ch in config.SIGNAL_CHANNELS:
            try:
                entity = await self._client.get_entity(ch)
                channels.append(entity)
                logger.info("Monitoring channel: %s", ch)
            except Exception:
                logger.error("Could not resolve channel: %s", ch)

        if not channels:
            raise RuntimeError("No valid channels to monitor")

        @self._client.on(events.NewMessage(chats=channels))
        async def handler(event):
            text = event.raw_text
            if not text:
                return

            if is_btc_trend_signal(text):
                logger.info("BTC trend notification, skipping: %s", text[:80])
                return

            signal = parse_signal(text)
            if signal is None:
                logger.debug("Unparseable message: %s", text[:80])
                return

            logger.info(
                "Signal: %s %s %.2f%% (🔔%d) [%s]",
                "🟢" if signal.is_pump else "🔴",
                signal.ticker,
                signal.abs_change,
                signal.bell_count,
                ", ".join(signal.exchanges),
            )

            try:
                await self._on_signal(signal)
            except Exception:
                logger.exception("Error processing signal for %s", signal.ticker)

        logger.info("Listening for signals…")
        await self._client.run_until_disconnected()

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            logger.info("Telegram client disconnected")
