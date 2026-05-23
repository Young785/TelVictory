"""Pluggable signal parsers per Telegram channel format."""
from dataclasses import dataclass
from typing import Optional, Union, Dict, Any

from signal_parser import TradingSignal

from . import lorenzo, binary_analyst, mt5_text

PARSERS = {
    'lorenzo': lorenzo.parse,
    'blackstar': lorenzo.parse,
    'binary_analyst': binary_analyst.parse,
    'mt5_text': mt5_text.parse,
}

PARSER_LABELS = {
    'lorenzo': 'Lorenzo / BlackStar',
    'blackstar': 'BlackStar',
    'binary_analyst': 'Binary Analyst',
    'mt5_text': 'MT5 text (alerts)',
}


@dataclass
class ParseOutcome:
    ok: bool
    parser_id: str
    signal: Optional[TradingSignal] = None
    mt5_alert: Optional[Dict[str, Any]] = None
    error: str = ''


def parse_message(message: str, parser_id: str = 'lorenzo', timezone_offset: int = 0) -> ParseOutcome:
    parser_id = (parser_id or 'lorenzo').lower()
    fn = PARSERS.get(parser_id)
    if not fn:
        return ParseOutcome(False, parser_id, error=f'Unknown parser: {parser_id}')

    if parser_id == 'mt5_text':
        alert = fn(message, timezone_offset)
        if alert:
            return ParseOutcome(True, parser_id, mt5_alert=alert)
        return ParseOutcome(False, parser_id, error='Not recognized as MT5 signal')

    sig = fn(message, timezone_offset)
    if sig:
        if not getattr(sig, 'parser_id', None):
            sig.parser_id = parser_id
        return ParseOutcome(True, parser_id, signal=sig)
    return ParseOutcome(False, parser_id, error='No signal found in message')
