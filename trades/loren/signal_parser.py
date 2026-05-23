from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timedelta
import re
import logging

@dataclass
class TradingSignal:
    pair: str
    expiration_minutes: int
    entry_time: str
    direction: str  # 'BUY' or 'SELL'
    martingale_levels: List[str]
    is_otc: bool
    received_at: datetime
    timezone_offset: int = field(default=0)  # Hours offset from UTC (e.g., -4 for UTC-4)
    entry_datetime: Optional[datetime] = field(default=None, init=False)
    parser_id: str = ''
    platform_id: str = 'pocket_option'
    source_channel: str = ''
    auto_trade: bool = True
    execute_immediate: bool = False  # Binary Analyst: trade now, no schedule

    def __post_init__(self):
        """Calculate entry_datetime once when signal is created."""
        self.entry_datetime = self._calculate_entry_datetime()
    
    def _calculate_entry_datetime(self) -> Optional[datetime]:
        """Convert entry time to local datetime, accounting for timezone.
        
        The signal entry time is in the SOURCE timezone (e.g., UTC+1 for BlackStar).
        We SUBTRACT the offset to convert to local time.
        Example: Signal says 12:55 in UTC+1, local is UTC+0, result is 11:55 UTC.
        """
        try:
            now = datetime.now()
            hour, minute = map(int, self.entry_time.split(':'))
            entry = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # Convert signal time (source timezone) to local time
            # If signal says 12:55 in UTC+1, and we're in UTC+0,
            # the actual entry time in our local time is 11:55
            entry = entry - timedelta(hours=self.timezone_offset)
            
            # Smart scheduling logic:
            # - If entry is in the future: use today
            # - If entry is in the past by < 10 minutes: use today (execute immediately)
            # - If entry is in the past by 10-180 minutes: use tomorrow (likely next occurrence)
            # - If entry is > 3 hours ago: use tomorrow
            time_diff_minutes = (entry - now).total_seconds() / 60
            
            if entry < now:
                minutes_ago = -time_diff_minutes
                if 10 < minutes_ago <= 180:
                    # Entry was 10-180 minutes ago, likely for tomorrow
                    entry = entry + timedelta(days=1)
                elif minutes_ago > 180:
                    # Entry was > 3 hours ago, assume tomorrow
                    entry = entry + timedelta(days=1)
                # If < 10 minutes ago, keep today (bot will execute immediately)
            
            return entry
        except Exception as e:
            logging.getLogger(__name__).error(f"Error calculating entry_datetime: {e}")
            return None


class SignalParser:
    """Parse trading signals from Telegram messages."""

    PATTERNS = {
        # Match currency pairs: EUR/USD, EURUSD, etc. (with or without flags)
        'pair': r'(?:🇺🇸|🇪🇺|🇬🇧|🇯🇵|🇨🇭|🇨🇦|🇦🇺|🇳🇿)?\s*(\w{3})[\s/]?(\w{3})\s*(?:🇺🇸|🇪🇺|🇬🇧|🇯🇵|🇨🇭|🇨🇦|🇦🇺|🇳🇿)?',
        # Match expiration: 5M, 5 Minutes, Expiry 5M, etc.
        'expiration': r'(?:Expiry|Expiration|Time|Duration)[\s:]*(\d+)\s*[Mm]?(?:inutes?)?',
        # Match entry time: 15:45, 15:45:00, entry 15:45, entry at 15:45, ⏺ Entry at 08:50, etc.
        'entry_time': r'(?:⏺\s*)?(?:Entry|Time)\s*(?:at\s+)?(\d{1,2}:\d{2}(?::\d{2})?)|(?:^|\s)at\s+(\d{1,2}:\d{2})',
        # Match direction: BUY, SELL, CALL, PUT, UP, DOWN with optional emojis
        'direction': r'(?:🟩|🟥|🔼|🔽|⬆️|⬇️|📈|📉)?\s*(BUY|SELL|CALL|PUT|UP|DOWN)\s*(?:🟩|🟥|🔼|🔽|⬆️|⬇️|📈|📉)?',
        # Match martingale levels: 1️⃣ level at 15:50, G1 15:50, 1 level at 15:50, level 1 at 15:50, etc.
        'martingale': r'(?:([1-3])[\s:]?(?:️⃣)?|G([1-3])|Level\s*([1-3]))[\s:]*(?:level\s*)?(?:at\s*)?(\d{2}:\d{2})',
        'otc': r'\bOTC\b',
    }

    def parse(self, message: str, timezone_offset: int = 0) -> Optional[TradingSignal]:
        """Parse a trading signal from message.
        
        Args:
            message: The signal message text
            timezone_offset: Hours offset from UTC for the signal time (e.g., -4 for UTC-4)
        """
        # Keep newlines for better parsing but also create a flattened version
        message_clean = message.replace('\n', ' ')

        logging = __import__('logging')
        logger = logging.getLogger(__name__)

        logger.debug(f"Parsing message: {repr(message[:200])}...")

        # Extract pair - look for XXX/YYY or XXXYYY pattern
        pair_match = re.search(self.PATTERNS['pair'], message, re.IGNORECASE)
        if not pair_match:
            # Try alternative: just look for 6 consecutive uppercase letters
            alt_match = re.search(r'\b([A-Z]{3})[/\s]?([A-Z]{3})\b', message_clean)
            if alt_match:
                pair = f"{alt_match.group(1)}/{alt_match.group(2)}"
                logger.debug(f"Found pair via alt method: {pair}")
            else:
                logger.warning(f"No currency pair found in message: {message[:100]}...")
                return None
        else:
            pair = f"{pair_match.group(1).upper()}/{pair_match.group(2).upper()}"

        # Extract expiration - look for number followed by M or Minutes
        expiration = 5  # Default to 5 minutes
        exp_match = re.search(self.PATTERNS['expiration'], message_clean, re.IGNORECASE)
        if exp_match:
            expiration = int(exp_match.group(1))
        else:
            # Try simpler pattern: just a number followed by M
            simple_exp = re.search(r'\b(\d+)\s*[Mm]\b', message_clean)
            if simple_exp:
                expiration = int(simple_exp.group(1))
            logger.debug(f"Using expiration: {expiration} minutes")

        # Extract entry time
        entry_time = None
        
        # Try multiple patterns in order of specificity
        entry_patterns = [
            # ⏺ Entry at 12:55 (Lorenzo/BlackStar format with emoji bullet)
            r'⏺\s*(?:Entry|Entry\s+at)\s+(?:at\s+)?(\d{1,2}:\d{2})',
            # Entry at 12:55 (standard format)
            r'(?:^|\s)[Ee]ntry\s+(?:at\s+)?(\d{1,2}:\d{2})',
            # ⏺ at 12:55 (just bullet + at)
            r'⏺\s+(?:at\s+)?(\d{1,2}:\d{2})',
            # "at HH:MM" but not martingale "level at HH:MM"
            r'(?<!level\s)at\s+(\d{1,2}:\d{2})',
            # First HH:MM in a line with "Entry" or time-related keywords
            r'(?:Entry|Time|⏺).*?(\d{1,2}:\d{2})',
        ]
        
        for pattern in entry_patterns:
            match = re.search(pattern, message_clean, re.IGNORECASE)
            if match:
                entry_time = match.group(1)
                logger.debug(f"Found entry time '{entry_time}' using pattern: {pattern[:50]}...")
                break
        
        # Fallback: search line by line for entry-related lines
        if not entry_time:
            lines = message.split('\n')
            for line in lines:
                if re.search(r'entry|⏺|time', line, re.IGNORECASE):
                    time_match = re.search(r'(\d{1,2}:\d{2})', line)
                    if time_match:
                        entry_time = time_match.group(1)
                        logger.debug(f"Found entry time '{entry_time}' via line search")
                        break

        if not entry_time:
            logger.warning(f"No entry time found in message. Clean: {message_clean[:150]}")
            return None

        # Extract direction
        direction = None
        direction_match = re.search(self.PATTERNS['direction'], message_clean, re.IGNORECASE)
        if direction_match:
            dir_raw = direction_match.group(1).upper()
            if dir_raw in ['BUY', 'CALL', 'UP']:
                direction = 'BUY'
            elif dir_raw in ['SELL', 'PUT', 'DOWN']:
                direction = 'SELL'

        if not direction:
            # Try emoji-based direction
            if re.search(r'🟩|⬆️|📈|🔼', message):
                direction = 'BUY'
            elif re.search(r'🟥|⬇️|📉|🔽', message):
                direction = 'SELL'
            else:
                logger.warning(f"No direction found in message: {message[:150]}...")
                return None

        # Extract martingale levels - look for multiple patterns
        martingale_levels = []
        martingale_matches = re.findall(self.PATTERNS['martingale'], message, re.IGNORECASE)
        for match in martingale_matches:
            # match is a tuple from the groups
            if match[3]:  # Time captured in group 4
                martingale_levels.append(match[3])

        # Also try simpler pattern: G1 15:50, G2 15:55
        simple_mart = re.findall(r'G?([1-3])[\s:]*(\d{2}:\d{2})', message_clean)
        for level, time in simple_mart:
            if time not in martingale_levels:
                martingale_levels.append(time)

        # BlackStar/Lorenzo format: 1️⃣ level at 08:55, 2️⃣ level at 09:00, etc.
        # Match keycap digit emojis (1️⃣, 2️⃣, 3️⃣) followed by optional text and time
        blackstar_patterns = [
            # 1️⃣ level at 08:55 or 1️⃣ at 08:55
            r'[1-3]\uFE0F?\u20E3?\s*(?:level\s+)?(?:at\s+)?(\d{2}:\d{2})',
            # Line starting with digit + emoji + time
            r'(?:^|\n)\s*[1-3][\s\uFE0F\u20E3]*\s*(?:level\s+)?(?:at\s+)?(\d{2}:\d{2})',
            # G1 15:50, G2 15:55 format
            r'G[1-3]\s+(\d{2}:\d{2})',
        ]
        
        for pattern in blackstar_patterns:
            matches = re.findall(pattern, message, re.IGNORECASE | re.MULTILINE)
            for time in matches:
                if time not in martingale_levels:
                    martingale_levels.append(time)
                    logger.debug(f"Found martingale time '{time}' using pattern")

        # Lorenzo format with 🔼/🔽 martingale header - just find all HH:MM patterns after martingale section
        if re.search(r'🔼|🔽|Martingale|MG|Gale', message, re.IGNORECASE):
            # Find all times after the martingale header
            lines = message.split('\n')
            in_martingale = False
            for line in lines:
                if re.search(r'🔼|🔽|Martingale|MG|Gale', line, re.IGNORECASE):
                    in_martingale = True
                    continue
                if in_martingale:
                    time_match = re.search(r'(\d{2}:\d{2})', line)
                    if time_match:
                        t = time_match.group(1)
                        if t not in martingale_levels:
                            martingale_levels.append(t)
                    # Stop if we hit empty line or non-martingale content
                    if re.search(r'💥|💰|GET|HOW|link|http', line, re.IGNORECASE):
                        break

        martingale_levels = sorted(set(martingale_levels))  # Remove duplicates and sort

        # Check if OTC
        is_otc = bool(re.search(self.PATTERNS['otc'], message, re.IGNORECASE))
        if not is_otc:
            # Check for OTC in the pair itself
            is_otc = 'OTC' in pair

        logger.info(f"✅ Parsed signal: {pair} {direction} at {entry_time} for {expiration}M (OTC: {is_otc}, Martingale: {len(martingale_levels)} levels)")
        logger.debug(f"Martingale times: {martingale_levels}")

        return TradingSignal(
            pair=pair,
            expiration_minutes=expiration,
            entry_time=entry_time,
            direction=direction,
            martingale_levels=martingale_levels,
            is_otc=is_otc,
            received_at=datetime.now(),
            timezone_offset=timezone_offset
        )
    
    def is_valid_signal(self, message: str) -> bool:
        return bool(self.parse(message))
