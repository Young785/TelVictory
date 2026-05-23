"""MT5 / Deriv channel text — alert-only, no binary execution."""
import re
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def parse(message: str, timezone_offset: int = 0) -> Optional[Dict[str, Any]]:
    """
    Examples:
        Crash 900 index sell. Use risk management
        Volatility 75 index buy
    Returns alert dict (not TradingSignal).
    """
    text = message.strip()
    lower = text.lower()
    if len(text) < 8:
        return None
    if re.search(r'close now|enjoy free|vip group|@\w+', lower):
        action = 'close_hint'
    else:
        action = 'open'

    side = None
    if re.search(r'\bsell\b', lower):
        side = 'SELL'
    elif re.search(r'\bbuy\b', lower):
        side = 'BUY'

    symbol = None
    m = re.search(r'(crash\s*\d+|volatility\s*\d+|boom\s*\d+|step\s*index\s*\d+)', lower)
    if m:
        symbol = m.group(1).upper().replace(' ', '')
    else:
        m2 = re.search(r'(\w+)\s*index', lower)
        if m2:
            symbol = m2.group(1).upper() + '_INDEX'

    if not symbol and action != 'close_hint':
        return None
    if action == 'open' and not side:
        return None

    return {
        'type': 'mt5_alert',
        'symbol': symbol or 'UNKNOWN',
        'direction': side,
        'action': action,
        'raw': text[:500],
        'parser_id': 'mt5_text',
    }
