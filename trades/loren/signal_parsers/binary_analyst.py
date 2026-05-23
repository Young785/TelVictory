"""TheBinaryAnalyst-style signals: asset + duration + direction (no Entry at)."""
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from signal_parser import TradingSignal

logger = logging.getLogger(__name__)

# Skip preamble / result messages
SKIP_PATTERNS = [
    r'get ready for signal',
    r'gained?\b',
    r'^\s*win\b',
    r'profit',
    r'http',
]


def parse(message: str, timezone_offset: int = 0) -> Optional[TradingSignal]:
    """
    Example:
        Get ready for signal
        Crypto IDX
        2 mins
        Buy
    """
    text = message.strip()
    lower = text.lower()
    if any(re.search(p, lower, re.I) for p in SKIP_PATTERNS if p != SKIP_PATTERNS[0]):
        if not re.search(r'\b(buy|sell|call|put)\b', lower):
            return None
    if 'get ready' in lower and not re.search(r'\b(buy|sell)\b', lower, re.I):
        return None

    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    if len(lines) < 2:
        return None

    direction = None
    expiration = 2
    asset = None

    for i, line in enumerate(lines):
        ll = line.lower()
        if re.match(r'^(buy|sell|call|put|up|down)$', ll):
            direction = 'BUY' if ll in ('buy', 'call', 'up') else 'SELL'
            continue
        dur = re.search(r'^(\d+)\s*min', ll)
        if dur:
            expiration = int(dur.group(1))
            continue
        if re.search(r'get ready|signal|gained|http', ll):
            continue
        if not direction and not dur:
            idx = re.search(r'(crypto\s*idx|volatility\s*\d+|crash\s*\d+)', ll, re.I)
            if idx:
                asset = idx.group(1).upper().replace(' ', '_')
            else:
                pair_m = re.search(r'\b([A-Z]{3})[/\s]?([A-Z]{3})\b', line, re.I)
                if pair_m:
                    asset = f"{pair_m.group(1).upper()}/{pair_m.group(2).upper()}"
                elif re.match(r'^[A-Za-z0-9_\s]{3,30}$', line) and not re.search(r'level|entry|martingale', ll):
                    asset = line.upper().replace(' ', '_')

    if not asset or not direction:
        return None

    is_otc = 'OTC' in text.upper() or 'OTC' in asset
    now = datetime.now()
    # Immediate entry — round up to next minute
    entry_dt = now + timedelta(seconds=30)
    entry_time = entry_dt.strftime('%H:%M')

    logger.info(f"✅ BinaryAnalyst parsed: {asset} {direction} {expiration}M (immediate)")

    sig = TradingSignal(
        pair=asset.replace('_', '/') if '/' not in asset and len(asset) == 6 else asset,
        expiration_minutes=expiration,
        entry_time=entry_time,
        direction=direction,
        martingale_levels=[],
        is_otc=is_otc,
        received_at=now,
        timezone_offset=timezone_offset,
    )
    sig.execute_immediate = True
    sig.parser_id = 'binary_analyst'
    return sig
