"""Lorenzo / BlackStar timed entry signals."""
from typing import Optional
from signal_parser import SignalParser, TradingSignal

_parser = SignalParser()


def parse(message: str, timezone_offset: int = 0) -> Optional[TradingSignal]:
    sig = _parser.parse(message, timezone_offset=timezone_offset)
    if sig:
        sig.parser_id = 'lorenzo'
    return sig
