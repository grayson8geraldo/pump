"""
Console listener — reads signals from stdin for manual input mode.

Usage: paste a signal like:
  🟢#WET:  + 3.88%. 🔔1
  ʙʏʙɪᴛ ʙɪɴᴀɴᴄᴇ ʙɪɴɢx ʙɪᴛɢᴇᴛ ᴍᴇxᴄ ᴏᴋx ɢᴀᴛᴇ

Then press Enter twice (empty line) to submit.
Type 'quit' or 'exit' to stop.
"""

import asyncio
import logging
import sys
from typing import Callable, Awaitable

from signal_parser import Signal, parse_signal, is_btc_trend_signal

logger = logging.getLogger(__name__)

SignalCallback = Callable[[Signal], Awaitable[None]]


class ConsoleListener:
    """Reads signals from console (stdin) instead of Telegram."""

    def __init__(self, on_signal: SignalCallback):
        self._on_signal = on_signal
        self._running = False

    async def start(self):
        """Start reading signals from stdin."""
        self._running = True
        logger.info("Console mode — paste signals below (empty line to submit, 'quit' to exit)")
        print()
        print("=" * 50)
        print("  PASTE SIGNAL AND PRESS ENTER TWICE TO SUBMIT")
        print("  Type 'quit' to exit")
        print("=" * 50)
        print()

        loop = asyncio.get_event_loop()

        while self._running:
            try:
                lines = []
                print("> ", end="", flush=True)

                while True:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    line = line.rstrip("\n")

                    if line.lower() in ("quit", "exit"):
                        logger.info("Exit command received")
                        self._running = False
                        return

                    if line == "" and lines:
                        # Empty line after content — submit
                        break
                    elif line == "" and not lines:
                        # Empty line with no content — skip
                        print("> ", end="", flush=True)
                        continue
                    else:
                        lines.append(line)

                text = "\n".join(lines)
                if not text.strip():
                    continue

                logger.info("Received input: %s", text[:100])

                if is_btc_trend_signal(text):
                    logger.info("BTC trend notification, skipping")
                    continue

                signal = parse_signal(text)
                if signal is None:
                    logger.warning("Could not parse signal from input. Check format.")
                    print("  [!] Failed to parse. Expected format:")
                    print("      🟢#WET:  + 3.88%. 🔔1")
                    print("      ʙʏʙɪᴛ ʙɪɴᴀɴᴄᴇ ...")
                    print()
                    continue

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

                print()
                print("-" * 50)
                print("  Ready for next signal...")
                print("-" * 50)
                print()

            except EOFError:
                self._running = False
                return
            except KeyboardInterrupt:
                self._running = False
                return

    async def stop(self):
        self._running = False
        logger.info("Console listener stopped")
