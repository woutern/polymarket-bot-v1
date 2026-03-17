"""Entry point for the trading bot."""

from __future__ import annotations

import asyncio
import signal
import sys

from polybot.config import Settings
from polybot.core.logging import setup_logging
from polybot.core.loop import TradingLoop


def main():
    settings = Settings()
    setup_logging(settings.log_level)

    loop_instance = TradingLoop(settings)

    async def run():
        # Graceful shutdown on SIGINT/SIGTERM
        stop_event = asyncio.Event()

        def handle_signal():
            stop_event.set()

        aloop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            aloop.add_signal_handler(sig, handle_signal)

        task = asyncio.create_task(loop_instance.start())

        # Wait for either shutdown signal OR the task itself to complete/fail
        done, _ = await asyncio.wait(
            [task, asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If the loop task crashed, log the exception and exit non-zero
        if task in done and task.exception():
            exc = task.exception()
            import structlog
            log = structlog.get_logger()
            log.error("loop_crashed_fatal", error=str(exc), exc_info=exc)
            await loop_instance.stop()
            sys.exit(1)

        await loop_instance.stop()
        task.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
