"""
Telegram Notifier - Send notifications for signals and trades.
Uses Telethon to send messages to a notification channel or user.
"""

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import PeerUser, PeerChannel

from config import Config
from database import log_event

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send trading notifications via Telegram."""
    
    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.client: Optional[TelegramClient] = None
        self.notification_target: Optional[str] = None
        self._initialized = False
        
    def _get_notification_target(self) -> Optional[str]:
        """Get the target for notifications (channel or user ID)."""
        # Priority: NOTIFICATION_CHANNEL_ID > TELEGRAM_CHANNEL_ID > user_id from session
        target = (
            os.getenv('NOTIFICATION_CHANNEL_ID') or 
            os.getenv('NOTIFICATION_USER_ID') or
            self.config.TELEGRAM_CHANNEL_ID
        )
        return target.strip() if target else None
    
    async def initialize(self):
        """Initialize the Telegram client for notifications."""
        if self._initialized:
            return True
            
        self.notification_target = self._get_notification_target()
        if not self.notification_target:
            logger.debug("No notification target configured")
            return False
        
        try:
            # Use the same session as the main bot
            self.client = TelegramClient(
                self.config.TELEGRAM_SESSION_NAME,
                self.config.TELEGRAM_API_ID,
                self.config.TELEGRAM_API_HASH
            )
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.warning("Telegram session not authorized - notifications disabled")
                await self.client.disconnect()
                return False
            
            self._initialized = True
            logger.info(f"Telegram notifier initialized - target: {self.notification_target}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize Telegram notifier: {e}")
            return False
    
    async def _send_message(self, message: str, parse_mode: str = 'markdown'):
        """Send a message to the notification target."""
        if not self._initialized or not self.client:
            return False
        
        try:
            target = self.notification_target
            
            # Parse target - could be username, channel ID, or user ID
            if target.startswith('@'):
                # Username
                entity = target
            elif target.lstrip('-').isdigit():
                # Numeric ID
                entity = int(target)
            else:
                # Try as username without @
                entity = target
            
            await self.client.send_message(entity, message, parse_mode=parse_mode)
            return True
            
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False
    
    def _format_signal_message(self, signal: Dict[str, Any], source_channel: str = "Unknown") -> str:
        """Format a signal detection notification."""
        pair = signal.get('pair', 'N/A')
        direction = signal.get('direction', 'N/A')
        expiration = signal.get('expiration', 'N/A')
        entry_time = signal.get('entry_time', 'N/A')
        martingale_levels = signal.get('martingale_levels', [])
        
        # Build message
        msg = f"🎯 **SIGNAL DETECTED**\n\n"
        msg += f"📊 **{pair}**\n"
        msg += f"📈 **Direction:** {direction}\n"
        msg += f"⏱ **Expiration:** {expiration}\n"
        msg += f"🕐 **Entry Time:** {entry_time}\n"
        
        if martingale_levels:
            msg += f"\n🔽 **Martingale Levels:**\n"
            for i, level in enumerate(martingale_levels, 1):
                msg += f"  {i}️⃣ {level}\n"
        
        msg += f"\n📡 **Source:** {source_channel}\n"
        msg += f"🕒 **Detected:** {datetime.now().strftime('%H:%M:%S')}"
        
        return msg
    
    def _format_trade_message(self, trade: Dict[str, Any], status: str = "PLACED") -> str:
        """Format a trade execution notification."""
        pair = trade.get('pair', 'N/A')
        direction = trade.get('direction', 'N/A')
        amount = trade.get('amount', 0)
        platform = trade.get('platform', 'Unknown')
        result = trade.get('result', 'pending')
        profit = trade.get('profit', 0)
        
        # Choose emoji based on status
        status_emoji = {
            'PLACED': '📤',
            'EXECUTED': '✅',
            'WIN': '🏆',
            'LOSS': '❌',
            'PENDING': '⏳',
            'ERROR': '⚠️'
        }.get(status, '📤')
        
        msg = f"{status_emoji} **TRADE {status}**\n\n"
        msg += f"📊 **{pair}**\n"
        msg += f"📈 **Direction:** {direction}\n"
        msg += f"💰 **Amount:** ${amount}\n"
        msg += f"🏛️ **Platform:** {platform}\n"
        
        if result != 'pending':
            result_emoji = '🟢' if result == 'win' else '🔴'
            msg += f"\n{result_emoji} **Result:** {result.upper()}\n"
            profit_emoji = '🟢' if profit > 0 else '🔴' if profit < 0 else '⚪'
            msg += f"{profit_emoji} **Profit:** ${profit:+.2f}\n"
        
        msg += f"\n🕒 **Time:** {datetime.now().strftime('%H:%M:%S')}"
        
        return msg
    
    async def notify_signal(self, signal: Dict[str, Any], source_channel: str = "Unknown"):
        """Send notification when a signal is detected."""
        await self.initialize()
        if not self._initialized:
            return False
        
        message = self._format_signal_message(signal, source_channel)
        success = await self._send_message(message)
        
        if success:
            log_event("info", f"📨 Signal notification sent for {signal.get('pair', 'N/A')}", "notifier")
        
        return success
    
    async def notify_trade_placed(self, trade: Dict[str, Any]):
        """Send notification when a trade is placed."""
        await self.initialize()
        if not self._initialized:
            return False
        
        message = self._format_trade_message(trade, 'PLACED')
        success = await self._send_message(message)
        
        if success:
            log_event("info", f"📨 Trade notification sent for {trade.get('pair', 'N/A')}", "notifier")
        
        return success
    
    async def notify_trade_result(self, trade: Dict[str, Any]):
        """Send notification when a trade result is known."""
        await self.initialize()
        if not self._initialized:
            return False
        
        result = trade.get('result', 'pending')
        status = 'WIN' if result == 'win' else 'LOSS' if result == 'loss' else 'EXECUTED'
        
        message = self._format_trade_message(trade, status)
        success = await self._send_message(message)
        
        if success:
            log_event("info", f"📨 Trade result notification sent: {result}", "notifier")
        
        return success
    
    async def notify_error(self, error_message: str, context: str = ""):
        """Send error notification."""
        await self.initialize()
        if not self._initialized:
            return False
        
        msg = f"⚠️ **ERROR ALERT**\n\n"
        if context:
            msg += f"📝 **Context:** {context}\n"
        msg += f"❌ **Error:** {error_message}\n"
        msg += f"\n🕒 **Time:** {datetime.now().strftime('%H:%M:%S')}"
        
        success = await self._send_message(msg)
        
        if success:
            log_event("error", f"📨 Error notification sent: {error_message}", "notifier")
        
        return success
    
    async def notify_bot_status(self, status: str, details: str = ""):
        """Send bot status notification (started/stopped)."""
        await self.initialize()
        if not self._initialized:
            return False
        
        emoji = '🟢' if status == 'STARTED' else '🔴' if status == 'STOPPED' else '🟡'
        
        msg = f"{emoji} **BOT {status}**\n\n"
        if details:
            msg += f"📝 {details}\n"
        msg += f"\n🕒 **Time:** {datetime.now().strftime('%H:%M:%S')}"
        
        return await self._send_message(msg)
    
    async def close(self):
        """Close the Telegram client."""
        if self.client:
            await self.client.disconnect()
            self._initialized = False


# Singleton instance
_notifier_instance: Optional[TelegramNotifier] = None


def get_notifier(config: Config = None) -> TelegramNotifier:
    """Get or create the singleton notifier instance."""
    global _notifier_instance
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier(config)
    return _notifier_instance


# Convenience functions for sync usage (fire and forget)
async def notify_signal_async(signal: Dict[str, Any], source_channel: str = "Unknown"):
    """Async: Send signal notification."""
    notifier = get_notifier()
    return await notifier.notify_signal(signal, source_channel)


async def notify_trade_async(trade: Dict[str, Any], status: str = "PLACED"):
    """Async: Send trade notification."""
    notifier = get_notifier()
    if status == 'PLACED':
        return await notifier.notify_trade_placed(trade)
    else:
        return await notifier.notify_trade_result(trade)


import os

# Backwards compatibility - fire and forget notification functions
def notify_signal(signal: Dict[str, Any], source_channel: str = "Unknown"):
    """Send signal notification (non-blocking)."""
    try:
        asyncio.create_task(notify_signal_async(signal, source_channel))
    except Exception as e:
        logger.debug(f"Failed to queue signal notification: {e}")


def notify_trade(trade: Dict[str, Any], status: str = "PLACED"):
    """Send trade notification (non-blocking)."""
    try:
        asyncio.create_task(notify_trade_async(trade, status))
    except Exception as e:
        logger.debug(f"Failed to queue trade notification: {e}")
