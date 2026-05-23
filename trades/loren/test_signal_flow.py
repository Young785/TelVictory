#!/usr/bin/env python3
"""Quick integration test for signal parse + trade scheduling."""
import asyncio
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_parser import SignalParser, TradingSignal
from trade_executor import TradeExecutor, parse_timezone_offset
from config import Config

LORENZO_MSG = """🇬🇧 GBP/AUD OTC
⏺ 5M
⏺ Entry at 16:25
🔴 SELL
🔼 Martingale
1️⃣ level at 16:30
2️⃣ level at 16:35
3️⃣ level at 16:40
"""


async def main():
    config = Config()
    parser = SignalParser()
    signal = parser.parse(LORENZO_MSG, timezone_offset=parse_timezone_offset(config.TIMEZONE))
    assert signal, "Parser failed"
    assert signal.pair == "GBP/AUD"
    assert signal.direction == "SELL"
    assert signal.is_otc
    print("OK parse:", signal.pair, signal.direction, "MG", signal.martingale_levels)

    now = datetime.now()
    entry = signal.entry_datetime
    diff = (entry - now).total_seconds() / 60
    schedule = diff > 2
    execute = -30 <= diff <= 2
    print(f"entry={entry.strftime('%H:%M')} diff={diff:.1f}m schedule={schedule} execute={execute}")

    executor = TradeExecutor(config)
    await executor.start()
    jobs_before = len(executor.scheduler.get_jobs())

    if schedule:
        executor.schedule_signal(signal)
    else:
        await executor.execute_trade_now(signal, 0, config.DEFAULT_AMOUNT)

    jobs_after = len(executor.scheduler.get_jobs())
    print(f"scheduler jobs: {jobs_before} -> {jobs_after}")
    for j in executor.scheduler.get_jobs():
        print(f"  job {j.id} at {j.next_run_time}")

    await executor.stop()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
