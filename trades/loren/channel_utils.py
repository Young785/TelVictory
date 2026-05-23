"""Channel profile helpers and schema defaults."""
from typing import Optional, Dict, Any
from database import channels_db
from trade_executor import parse_timezone_offset

PARSER_CHOICES = [
    ('lorenzo', 'Lorenzo / BlackStar (Entry at HH:MM, martingale)'),
    ('blackstar', 'BlackStar (same as Lorenzo)'),
    ('binary_analyst', 'Binary Analyst (Asset + N mins + Buy/Sell)'),
    ('mt5_text', 'MT5 / Deriv text (alert only)'),
]

PLATFORM_CHOICES = [
    ('pocket_option', 'Pocket Option'),
    ('deriv', 'Deriv'),
    ('none', 'No auto-trade (alerts only)'),
]

DEFAULT_CHANNEL_PROFILE = {
    'parser_id': 'lorenzo',
    'platform_id': 'pocket_option',
    'timezone': 'UTC',
    'auto_trade': True,
    'market_type': 'binary_timed',
    'default_amount': None,
}


def normalize_channel_record(ch: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure channel has all profile fields (migrate old records)."""
    out = dict(ch)
    for key, default in DEFAULT_CHANNEL_PROFILE.items():
        if out.get(key) is None and key != 'default_amount':
            out[key] = default
    if 'channel_name' not in out and out.get('name'):
        out['channel_name'] = out['name']
    if not out.get('channel_name'):
        out['channel_name'] = 'Unknown'
    return out


def get_all_channels_normalized():
    return [normalize_channel_record(ch) for ch in channels_db.get_all()]


def find_channel_by_chat_id(chat_id: str) -> Optional[Dict[str, Any]]:
    """Match Telegram chat id to configured channel."""
    clean = str(chat_id).replace('-100', '').replace('-', '')
    for ch in get_all_channels_normalized():
        cid = str(ch.get('channel_id', ''))
        cid_clean = cid.replace('-100', '').replace('-', '')
        if cid == str(chat_id) or cid_clean == clean:
            return ch
    return None


def channel_timezone_offset(ch: Dict[str, Any], global_tz: str) -> int:
    tz = ch.get('timezone') or global_tz or 'UTC'
    return parse_timezone_offset(tz)


def channel_trade_amount(ch: Dict[str, Any], default: float) -> float:
    amt = ch.get('default_amount')
    if amt is not None and amt != '':
        try:
            return float(amt)
        except (TypeError, ValueError):
            pass
    return default
