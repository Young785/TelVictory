import logging
import asyncio
import re
import os
import json
import fcntl
import atexit
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import Channel
from telethon.tl.functions.messages import ImportChatInviteRequest

from trade_executor import TradeExecutor, parse_timezone_offset
from config import Config
from database import channels_db, signals_db, log_event
from channel_utils import (
    find_channel_by_chat_id,
    get_all_channels_normalized,
    channel_timezone_offset,
    channel_trade_amount,
    normalize_channel_record,
)
from signal_parsers import parse_message, PARSER_LABELS
from telegram_notifier import notify_signal

# Global file lock to prevent multiple bot instances from running simultaneously
_lock_file = None

def _acquire_lock():
    """Acquire exclusive lock to prevent multiple bot instances."""
    global _lock_file
    lock_path = os.path.join(os.path.dirname(__file__), '.bot.lock')
    _lock_file = open(lock_path, 'w')
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (IOError, OSError):
        _lock_file.close()
        _lock_file = None
        return False

def _release_lock():
    """Release the file lock on exit."""
    global _lock_file
    if _lock_file:
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
            _lock_file.close()
        except:
            pass
        _lock_file = None

atexit.register(_release_lock)


def validate_phone_number(phone: str) -> bool:
    """Validate phone number format (E.164)."""
    if not phone:
        return False
    # Remove spaces and common separators
    phone = phone.strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    # Must start with + followed by country code and digits
    pattern = r'^\+[1-9]\d{1,14}$'
    return bool(re.match(pattern, phone))


def prompt_for_phone() -> str:
    """Prompt user for phone number with validation."""
    print("\n📱 Telegram Authentication Required")
    print("=" * 50)
    print("Phone number must include country code (e.g., +2348061163188)")
    print("=" * 50)

    while True:
        try:
            phone = input("Please enter your phone (or bot token): ").strip()

            # Bot tokens are different format (123456:ABC-DEF...), let those pass
            if ':' in phone:
                return phone

            # Auto-add + if missing and starts with digit
            if not phone.startswith('+') and phone[0].isdigit():
                phone = '+' + phone
                print(f"Auto-formatted: {phone}")

            if validate_phone_number(phone):
                return phone
            else:
                print("\n❌ Invalid phone number!")
                print("   Format: +[country_code][number]")
                print("   Example: +2348061163188 (Nigeria)")
                print("   Example: +14155552671 (USA)")
                print("   Example: +447911123456 (UK)\n")
        except (EOFError, KeyboardInterrupt):
            print("\n\nAuthentication cancelled.")
            return None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TradingUserBot:
    """Telethon Userbot that listens for trading signals and executes trades."""
    
    def __init__(self):
        self.config = Config()
        self.executor: TradeExecutor = None
        self.client: TelegramClient = None
        self.timezone_offset = parse_timezone_offset(self.config.TIMEZONE)
        self._monitored_chat_ids: set = set()  # Store IDs of monitored private channels
    
    def setup(self):
        """Initialize Telethon client with SQLite timeout to prevent locking."""
        import sqlite3
        from telethon.sessions import SQLiteSession
        
        session_file = f"{self.config.TELEGRAM_SESSION_NAME}.session"
        session_exists = os.path.exists(session_file)
        
        logger.info(f"📱 Session setup: {'Using existing' if session_exists else 'Creating new'} session")
        
        # Create session with extended SQLite timeout to prevent "database is locked" errors
        # This is crucial when multiple threads access the session database
        session = SQLiteSession(self.config.TELEGRAM_SESSION_NAME)
        
        # Configure SQLite connection with busy timeout (waits up to 30 seconds if locked)
        try:
            conn = sqlite3.connect(session_file, timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")  # 30 seconds
            conn.execute("PRAGMA journal_mode = WAL")   # Write-Ahead Logging for better concurrency
            conn.close()
            logger.debug(f"✅ SQLite configured with 30s busy timeout + WAL mode")
        except Exception as e:
            logger.debug(f"Session DB not yet created or accessible: {e}")
        
        # Get proxy settings if configured
        proxy = None
        if hasattr(self.config, 'TELEGRAM_PROXY') and self.config.TELEGRAM_PROXY:
            # Format: socks5://user:pass@host:port or http://host:port
            proxy = self.config.TELEGRAM_PROXY
        
        self.client = TelegramClient(
            session,
            self.config.TELEGRAM_API_ID,
            self.config.TELEGRAM_API_HASH,
            timeout=30,  # Connection timeout
            connection_retries=5,  # Increased from 3 to 5
            retry_delay=1,  # Initial retry delay in seconds
            auto_reconnect=True,  # Automatically reconnect on disconnect
            proxy=proxy
        )
        
        # Register event handler for new messages
        @self.client.on(events.NewMessage)
        async def handle_new_message(event):
            await self._process_message(event)
    
    def _get_active_channels(self):
        """Get list of active channel IDs from database."""
        channels = channels_db.get_all()
        active = []
        for ch in channels:
            if ch.get('is_active', True):
                ch_id = ch.get('channel_id', '')
                active.append(ch_id)
                # Also add to monitored set for quick lookup (with -100 prefix normalization)
                if ch_id:
                    self._monitored_chat_ids.add(ch_id)
                    # Also add without -100 prefix if present
                    clean_id = ch_id.replace('-100', '').replace('-', '')
                    if clean_id != ch_id:
                        self._monitored_chat_ids.add(clean_id)
        # Fallback to env config if no channels in db
        if not active and self.config.TELEGRAM_CHANNEL_ID:
            active.append(self.config.TELEGRAM_CHANNEL_ID)
        logger.debug(f"Active channels from DB: {active}, monitored IDs: {self._monitored_chat_ids}")
        return active

    def _is_monitored_channel(self, chat, active_channels):
        """Check if chat is in the monitored channels list."""
        # If MONITOR_ALL_CHANNELS is set, monitor all channels
        if self.config.MONITOR_ALL_CHANNELS:
            return True
        # If no channels configured, monitor all
        if not active_channels:
            return True

        chat_username = getattr(chat, 'username', '') or ''
        chat_id = str(getattr(chat, 'id', ''))

        # Check if this chat ID is in our monitored set (for private channels we've joined)
        # Strip -100 prefix from both sides for comparison (Telegram uses -100 prefix for channels)
        clean_chat_id = chat_id.replace('-100', '').replace('-', '')
        monitored_clean = {cid.replace('-100', '').replace('-', '') for cid in self._monitored_chat_ids}
        logger.debug(f"Checking channel: chat_id={chat_id}, clean={clean_chat_id}, monitored={self._monitored_chat_ids}, monitored_clean={monitored_clean}")
        if chat_id in self._monitored_chat_ids or clean_chat_id in monitored_clean:
            chat_title = getattr(chat, 'title', 'Unknown')
            logger.debug(f"Channel {chat_title} matched by ID")
            return True

        for target in active_channels:
            target = target.strip()
            if not target:
                continue

            # Handle private invite links - we should have already stored the chat ID when joining
            if 't.me/+' in target or target.startswith('+'):
                # For private channels, we match by stored chat ID (set when joining)
                # The invite hash is not useful for matching after joining
                continue

            # Match by username
            if target.startswith('@'):
                if target[1:].lower() == chat_username.lower():
                    # Store this chat ID for future lookups
                    self._monitored_chat_ids.add(chat_id)
                    return True
            # Match by ID (with or without -100 prefix)
            elif target.lstrip('-').isdigit():
                clean_target = target.replace('-100', '').replace('-', '')
                clean_chat_id = chat_id.replace('-100', '').replace('-', '')
                if clean_target == clean_chat_id:
                    self._monitored_chat_ids.add(chat_id)
                    return True
            # Match username without @
            elif target.lower() == chat_username.lower():
                self._monitored_chat_ids.add(chat_id)
                return True

        chat_title = getattr(chat, 'title', 'Unknown')
        logger.warning(f"Channel {chat_title} (ID: {chat_id}, username: {chat_username}) not in monitored list: {active_channels}")
        return False

    async def _process_message(self, event, is_backlog=False):
        """Process incoming messages for trading signals.
        
        Args:
            event: Telethon NewMessage event
            is_backlog: True if this is a backlog message (recent history on reconnect)
        """
        # Skip if no message
        if not event.message:
            return

        # Get message text or caption (for media messages)
        message = event.message.text or event.message.message
        if not message:
            # Log skipped media-only messages for debugging
            try:
                chat = await event.get_chat()
                chat_name = getattr(chat, 'title', 'Unknown')
                media_type = 'voice' if event.message.voice else 'photo' if event.message.photo else 'video' if event.message.video else 'media'
                logger.debug(f"📎 Skipping {media_type}-only message from {chat_name} (no text caption)")
            except:
                pass
            return

        # Fetch chat info
        chat = await event.get_chat()
        chat_name = getattr(chat, 'title', 'Unknown')
        chat_username = getattr(chat, 'username', '') or 'N/A'
        chat_id = str(getattr(chat, 'id', ''))

        # Get message timestamp for debugging
        msg_date = event.message.date
        local_now = datetime.now()
        time_diff = (local_now - msg_date.replace(tzinfo=None)).total_seconds() / 60 if msg_date else 0

        # Only process messages from channels/groups
        if not isinstance(chat, Channel):
            logger.debug(f"[{chat_name}] Ignoring non-channel message")
            return

        # Log every message received for debugging
        backlog_tag = "[BACKLOG] " if is_backlog else ""
        time_tag = f"[{time_diff:.0f}min ago] " if time_diff > 1 else ""
        logger.info(f"📩 {backlog_tag}{time_tag}Message from {chat_name} (@{chat_username}, ID: {chat_id}): {message[:100]}...")
        log_event("info", f"📩 {backlog_tag}Message from {chat_name}: {message[:100]}...", "bot")

        # Check if message is from configured channels
        active_channels = self._get_active_channels()
        logger.debug(f"Active channels to monitor: {active_channels}")
        
        # Always monitor all channels if MONITOR_ALL_CHANNELS is set (default: true)
        if self.config.MONITOR_ALL_CHANNELS:
            logger.debug(f"✅ Monitoring all channels mode - processing message from {chat_name}")
            is_monitored = True
        elif active_channels:
            is_monitored = self._is_monitored_channel(chat, active_channels)
            logger.debug(f"Channel {chat_name} monitored: {is_monitored}")
            if not is_monitored:
                logger.debug(f"❌ Ignoring message from {chat_name} (@{chat_username}, ID: {chat_id}), not in monitored channels: {active_channels}")
                return
        else:
            # No channels configured, monitor all by default
            is_monitored = True

        logger.info(f"✅ Channel {chat_name} is being monitored, checking for signal...")
        log_event("info", f"✅ Channel {chat_name} is being monitored, checking for signal...", "bot")

        # Channel profile (parser, platform, timezone)
        ch_profile = find_channel_by_chat_id(chat_id)
        parser_id = (ch_profile or {}).get('parser_id', 'lorenzo')
        tz_offset = channel_timezone_offset(ch_profile, self.config.TIMEZONE) if ch_profile else self.timezone_offset
        platform_id = (ch_profile or {}).get('platform_id', 'pocket_option')
        auto_trade = (ch_profile or {}).get('auto_trade', True)
        trade_amount = channel_trade_amount(ch_profile, self.config.DEFAULT_AMOUNT) if ch_profile else self.config.DEFAULT_AMOUNT

        outcome = parse_message(message, parser_id=parser_id, timezone_offset=tz_offset)
        if not outcome.ok:
            logger.info(f"⚠️ Message from {chat_name} not recognized ({parser_id}): {outcome.error}")
            log_event("info", f"⚠️ Not a signal from {chat_name} [{parser_id}]", "bot")
            return

        if outcome.mt5_alert:
            alert = outcome.mt5_alert
            log_event(
                "info",
                f"📋 MT5 ALERT from {chat_name}: {alert.get('symbol')} {alert.get('direction')} ({alert.get('action')})",
                "bot",
            )
            signals_db.insert({
                'pair': alert.get('symbol'),
                'direction': alert.get('direction') or 'N/A',
                'entry_time': 'alert',
                'source': chat_name,
                'status': 'mt5_alert',
                'parser_id': 'mt5_text',
                'platform_id': 'none',
                'raw_message': alert.get('raw', '')[:300],
            })
            logger.info(f"MT5 alert logged (no auto-trade): {alert}")
            return

        signal = outcome.signal
        if not signal:
            return

        signal.platform_id = platform_id
        signal.source_channel = chat_name
        signal.auto_trade = auto_trade and platform_id not in ('none', 'mt5')
        if not signal.auto_trade:
            log_event(
                "info",
                f"📋 SIGNAL (monitor only) from {chat_name}: {signal.pair} {signal.direction} — auto-trade off",
                "bot",
            )
            signals_db.insert({
                'pair': signal.pair,
                'direction': signal.direction,
                'entry_time': signal.entry_time,
                'source': chat_name,
                'status': 'detected_no_trade',
                'parser_id': parser_id,
                'platform_id': platform_id,
            })
            return

        logger.info("="*60)
        logger.info(f"🎯 SIGNAL DETECTED from {chat_name}")
        logger.info(f"   Asset: {signal.pair} | Direction: {signal.direction}")
        logger.info(f"   Entry: {signal.entry_time} | Expires: {signal.expiration_minutes}M")
        logger.info(f"   OTC: {signal.is_otc} | Martingale: {len(signal.martingale_levels)} levels")
        if signal.martingale_levels:
            logger.info(f"   MG Times: {', '.join(signal.martingale_levels)}")
        logger.info("="*60)
        log_event(
            "info",
            f"🎯 SIGNAL [{parser_id}] from {chat_name}: {signal.pair} {signal.direction} at {signal.entry_time} → {platform_id}",
            "bot",
        )
        
        # Send Telegram notification for the signal
        signal_dict = {
            'pair': signal.pair,
            'direction': signal.direction,
            'entry_time': signal.entry_time,
            'expiration': f"{signal.expiration_minutes}M",
            'martingale_levels': signal.martingale_levels,
            'is_otc': signal.is_otc
        }
        notify_signal(signal_dict, chat_name)

        # Determine if we should trade immediately or schedule
        now = datetime.now()
        entry_dt = signal.entry_datetime
        
        if not entry_dt:
            logger.error(f"❌ Could not parse entry time: {signal.entry_time}")
            return

        time_diff_minutes = (entry_dt - now).total_seconds() / 60

        if getattr(signal, 'execute_immediate', False):
            schedule_for_later = False
            execute_immediately = True
            signal_expired = False
        else:
            schedule_for_later = time_diff_minutes > 2
            execute_immediately = -30 <= time_diff_minutes <= 2
            signal_expired = time_diff_minutes < -30

        # Check if signal has already expired
        if signal_expired:
            logger.warning(f"⏰ Signal EXPIRED: Entry time {signal.entry_time} was {-time_diff_minutes:.0f} minutes ago. Skipping trade.")
            log_event("warning", f"⏰ Expired signal from {chat_name}: {signal.pair} {signal.direction} at {signal.entry_time} (was {-time_diff_minutes:.0f} min ago)", "bot")
            # Still save to database for record
            signals_db.insert({
                'pair': signal.pair,
                'direction': signal.direction,
                'entry_time': signal.entry_time,
                'expiration_minutes': signal.expiration_minutes,
                'is_otc': signal.is_otc,
                'martingale_levels': signal.martingale_levels,
                'source': chat_name,
                'status': 'expired',
                'scheduled_at': datetime.now().isoformat(),
                'expired_minutes_ago': -time_diff_minutes
            })
            return

        # Save signal to database (entry_execute_at used to re-schedule after bot restart)
        signals_db.insert({
            'pair': signal.pair,
            'direction': signal.direction,
            'entry_time': signal.entry_time,
            'entry_execute_at': entry_dt.isoformat(),
            'expiration_minutes': signal.expiration_minutes,
            'is_otc': signal.is_otc,
            'martingale_levels': signal.martingale_levels,
            'source': chat_name,
            'status': 'executing' if execute_immediately else 'scheduled',
            'scheduled_at': datetime.now().isoformat(),
            'parser_id': parser_id,
            'platform_id': platform_id,
        })
        logger.info(f"💾 Signal saved to database: {signal.pair} {signal.direction}")

        if execute_immediately:
            logger.info(f"⚡ EXECUTING IMMEDIATELY! Entry {signal.entry_time} is {time_diff_minutes:+.1f} min from now")
            logger.info(f"   Main trade: ${trade_amount} on {signal.pair}")
            log_event("info", f"⚡ Executing trade now: {signal.pair} {signal.direction} at {signal.entry_time}", "bot")
            await self.executor.execute_trade_now(signal, 0, trade_amount)
            if signal.martingale_levels:
                logger.info(f"   Scheduling {len(signal.martingale_levels)} martingale levels...")
                self.executor.schedule_martingale_only(signal)
        elif schedule_for_later:
            logger.info(f"⏰ SCHEDULING FOR LATER: Entry {signal.entry_time} is {time_diff_minutes:.1f} min away")
            logger.info(f"   Main trade + {len(signal.martingale_levels)} MG levels will execute at exact time")
            log_event("info", f"⏰ Scheduled: {signal.pair} {signal.direction} at {entry_dt.strftime('%H:%M')}", "bot")
            self.executor.schedule_signal(signal)

        # Optional: Send confirmation reply (if we have permission)
        try:
            await event.reply(
                f"✅ Signal scheduled!\n"
                f"{signal.pair} {signal.direction}\n"
                f"Entry: {signal.entry_time}\n"
                f"Martingale: {len(signal.martingale_levels)} levels"
            )
        except Exception as e:
            logger.debug(f"Could not send reply: {e}")
    
    async def run(self):
        """Start the userbot."""
        # Startup banner
        logger.info("="*60)
        logger.info("🤖 Trading Bot Starting...")
        logger.info(f"📊 Mode: Telegram Signal Monitor + Auto-Trading")
        logger.info(f"⏰ Timezone: {self.config.TIMEZONE} (offset: {self.timezone_offset}h)")
        logger.info(f"💰 Trade Amount: ${self.config.DEFAULT_AMOUNT}")
        logger.info(f"📈 Max Martingale: {self.config.MAX_MARTINGALE_LEVELS} levels")
        logger.info("="*60)
        
        # Validate config
        missing = Config.validate()
        if missing:
            logger.error(f"Missing required configuration: {', '.join(missing)}")
            print(f"\n⚠️  Missing required environment variables: {', '.join(missing)}")
            print("Get API_ID and API_HASH from https://my.telegram.org")
            print("Create a .env file with these variables.\n")
            return
        
        # Initialize executor (reuse Web UI instance when started from dashboard)
        if self.executor is None:
            self.executor = TradeExecutor(self.config)
            await self.executor.start()
        elif self.executor.scheduler is None:
            await self.executor.start()
        else:
            logger.info("Reusing existing TradeExecutor from Web UI")

        # Setup client
        self.setup()

        # Check if session exists, if not we need to handle auth with validation
        session_file = f"{self.config.TELEGRAM_SESSION_NAME}.session"
        phone = None
        auth_request_file = os.path.join(os.path.dirname(__file__), '.telegram_auth_request.json')
        
        if not os.path.exists(session_file):
            logger.info("No existing session found. Authentication required.")
            
            # Check if there's an auth request from Web UI
            if os.path.exists(auth_request_file):
                try:
                    with open(auth_request_file, 'r') as f:
                        auth_data = json.load(f)
                    phone = auth_data.get('phone', '')
                    if phone:
                        logger.info(f"📱 Using phone from Web UI auth request: {phone[:4]}...{phone[-4:]}")
                        # Remove the file so we don't reuse it
                        os.remove(auth_request_file)
                except Exception as e:
                    logger.warning(f"Could not read auth request file: {e}")
            
            # If no auth request, prompt interactively
            if not phone:
                phone = prompt_for_phone()
                if not phone:
                    logger.error("Phone number not provided. Cannot continue.")
                    return

        logger.info("Connecting to Telegram...")
        max_retries = 5
        retry_count = 0
        connected = False
        session_reset_done = False
        
        while retry_count < max_retries and not connected:
            try:
                if phone:
                    await self.client.start(phone=phone)
                else:
                    await self.client.start()
                connected = True
            except Exception as conn_error:
                retry_count += 1
                error_msg = str(conn_error).lower()
                logger.warning(f"Connection attempt {retry_count}/{max_retries} failed: {str(conn_error)[:100]}")
                
                # Auto-fix: Detect database lock or session corruption
                is_db_locked = "database is locked" in error_msg
                is_session_error = "wrong session" in error_msg or "session" in error_msg and ("invalid" in error_msg or "expired" in error_msg)
                
                if is_db_locked or is_session_error:
                    if not session_reset_done and os.path.exists(session_file):
                        logger.warning("🔧 Detected session/database corruption. Auto-resetting session...")
                        try:
                            # Close current client if possible
                            try:
                                await self.client.disconnect()
                            except:
                                pass
                            # Remove corrupted session
                            os.remove(session_file)
                            logger.info("✅ Corrupted session file removed. Will recreate on next attempt.")
                            session_reset_done = True
                            retry_count = 0  # Reset retry counter since we're starting fresh
                            # Re-setup the client with fresh session
                            self.setup()
                            phone = prompt_for_phone()
                            if not phone:
                                logger.error("Phone number required to recreate session.")
                                return
                            continue  # Skip the wait, try immediately with new session
                        except Exception as cleanup_err:
                            logger.error(f"Failed to reset session: {cleanup_err}")
                
                if "two different ip" in error_msg or "authorization key" in error_msg:
                    log_event(
                        "error",
                        "Telegram session invalidated (used on two IPs). Re-authenticate from ONE server only.",
                        "bot"
                    )
                    logger.error("❌ Telegram session conflict: stop all other bot instances and re-auth via Telegram Auth")

                if retry_count < max_retries:
                    wait_time = min(2 ** retry_count, 30)  # Exponential backoff, max 30s
                    logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"❌ Failed to connect to Telegram after {max_retries} attempts")
                    logger.error(f"Error details: {error_msg}")
                    logger.error("")
                    logger.error("🔧 TROUBLESHOOTING:")
                    logger.error("   1. No internet connection - check your network")
                    logger.error("   2. Telegram API blocked - try using a VPN")
                    logger.error("   3. Invalid API_ID or API_HASH - verify at https://my.telegram.org")
                    logger.error("   4. Rate limiting - wait 5 minutes and try again")
                    logger.error("   5. Session corruption - delete loren_session.session manually")
                    logger.error("")
                    logger.error("💡 To reset everything: rm loren_session.session .bot.lock")
                    raise
        
        # Log connection info
        me = await self.client.get_me()
        logger.info(f"Connected as {me.first_name} (@{me.username})")
        
        # Get target channel info if configured
        if self.config.TELEGRAM_CHANNEL_ID:
            try:
                channel_id = self.config.TELEGRAM_CHANNEL_ID
                
                # Handle private channel invite links (t.me/+hash or +hash)
                invite_hash = None
                if 't.me/+' in channel_id:
                    invite_hash = channel_id.split('+')[-1].split('/')[-1]
                elif channel_id.startswith('+'):
                    invite_hash = channel_id[1:]
                
                if invite_hash:
                    # Try to join private channel using user's account
                    try:
                        logger.info(f"Attempting to join private channel with invite: {invite_hash}")
                        result = await self.client(ImportChatInviteRequest(invite_hash))
                        logger.info(f"Successfully joined channel!")
                        # Get the channel entity
                        channel = result.updates[0].chat if hasattr(result, 'updates') else None
                        if channel:
                            chat_id = str(getattr(channel, 'id', ''))
                            if chat_id:
                                self._monitored_chat_ids.add(chat_id)
                                logger.info(f"Monitoring channel: {getattr(channel, 'title', 'Unknown')} (ID: {chat_id})")
                        else:
                            channel_id = None  # Monitor all channels if we can't get specific channel
                    except Exception as join_error:
                        if "already a participant" in str(join_error).lower() or "user already" in str(join_error).lower():
                            logger.info("Already a member of this channel")
                            # Try to get channel by invite link
                            try:
                                from telethon.tl.functions.messages import CheckChatInviteRequest
                                invite_info = await self.client(CheckChatInviteRequest(invite_hash))
                                if hasattr(invite_info, 'chat'):
                                    channel = invite_info.chat
                                    chat_id = str(getattr(channel, 'id', ''))
                                    if chat_id:
                                        self._monitored_chat_ids.add(chat_id)
                                        logger.info(f"Monitoring channel: {getattr(channel, 'title', 'Unknown')} (ID: {chat_id})")
                            except Exception as e2:
                                logger.warning(f"Could not get channel info: {e2}")
                                channel_id = None
                        else:
                            logger.warning(f"Could not join channel: {join_error}")
                            logger.info("You may need to join the channel manually first")
                            channel_id = None
                else:
                    # Handle regular username or numeric ID
                    if channel_id.lstrip('-').isdigit():
                        channel_id = int(channel_id)
                    channel = await self.client.get_entity(channel_id)
                    chat_id = str(getattr(channel, 'id', ''))
                    if chat_id:
                        self._monitored_chat_ids.add(chat_id)
                    logger.info(f"Monitoring channel: {channel.title} (@{getattr(channel, 'username', 'N/A')}) (ID: {chat_id})")
            except Exception as e:
                logger.warning(f"Could not resolve channel {self.config.TELEGRAM_CHANNEL_ID}: {e}")
        else:
            logger.info("Monitoring ALL channels (set TELEGRAM_CHANNEL_ID to filter)")
        
        logger.info("Bot is running and listening for signals...")
        logger.info("Press Ctrl+C to stop")
        
        # Keep running until interrupted with auto-reconnect handling
        while True:
            try:
                await self.client.run_until_disconnected()
                break  # Normal disconnect (Ctrl+C)
            except Exception as e:
                error_str = str(e).lower()
                if "connection" in error_str or "network" in error_str or "timeout" in error_str:
                    logger.warning(f"Connection lost: {e}")
                    logger.info("Attempting to reconnect in 5 seconds...")
                    await asyncio.sleep(5)
                    try:
                        if not self.client.is_connected():
                            await self.client.connect()
                            logger.info("Reconnected successfully!")
                            continue
                    except Exception as reconnect_error:
                        logger.error(f"Reconnection failed: {reconnect_error}")
                        break
                else:
                    logger.error(f"Unexpected error: {e}")
                    raise
        
        # Cleanup
        try:
            await self.executor.stop()
            await self.client.disconnect()
        except:
            pass
        logger.info("Bot stopped")


def main():
    # Check if another bot instance is already running
    if not _acquire_lock():
        print("❌ Another bot instance is already running. Only one instance allowed.")
        print("   Stop the existing bot first before starting a new one.")
        return
    
    bot = TradingUserBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
