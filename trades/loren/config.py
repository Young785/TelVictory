import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    # Telegram (Telethon Userbot - Get from https://my.telegram.org)
    TELEGRAM_API_ID: int = int(os.getenv('TELEGRAM_API_ID', '0'))
    TELEGRAM_API_HASH: str = os.getenv('TELEGRAM_API_HASH', '')
    TELEGRAM_SESSION_NAME: str = os.getenv('TELEGRAM_SESSION_NAME', 'loren_session')
    TELEGRAM_CHANNEL_ID: str = os.getenv('TELEGRAM_CHANNEL_ID', '')  # Channel username or ID
    TELEGRAM_PROXY: str = os.getenv('TELEGRAM_PROXY', '')  # Optional: socks5://host:port or http://host:port
    
    # Trading Platform (Pocket Option)
    POCKET_OPTION_EMAIL: str = os.getenv('POCKET_OPTION_EMAIL', '')
    POCKET_OPTION_PASSWORD: str = os.getenv('POCKET_OPTION_PASSWORD', '')
    POCKET_OPTION_SSID: str = os.getenv('POCKET_OPTION_SSID', '')  # Session token from browser
    
    # Trading Settings
    DEFAULT_AMOUNT: float = float(os.getenv('DEFAULT_AMOUNT', '10.0'))  # Trade amount
    CURRENCY: str = os.getenv('CURRENCY', 'USD')
    MAX_MARTINGALE_LEVELS: int = int(os.getenv('MAX_MARTINGALE_LEVELS', '3'))
    MARTINGALE_MULTIPLIER: float = float(os.getenv('MARTINGALE_MULTIPLIER', '2.5'))
    TIMEZONE: str = os.getenv('TIMEZONE', 'UTC')  # e.g., 'UTC-4', 'America/New_York', 'Europe/London'
    
    # Bot Behavior
    MONITOR_ALL_CHANNELS: bool = os.getenv('MONITOR_ALL_CHANNELS', 'false').lower() in ('true', '1', 'yes', 'on')

    # Email reports (SMTP)
    REPORT_EMAIL_ENABLED: bool = os.getenv('REPORT_EMAIL_ENABLED', 'true').lower() in ('true', '1', 'yes', 'on')
    REPORT_EMAIL_TO: str = os.getenv('REPORT_EMAIL_TO', 'ayomikunariyo@gmail.com')
    SMTP_HOST: str = os.getenv('SMTP_HOST', '')
    SMTP_PORT: int = int(os.getenv('SMTP_PORT', '587'))
    SMTP_USER: str = os.getenv('SMTP_USER', '')
    SMTP_PASSWORD: str = os.getenv('SMTP_PASSWORD', '')
    SMTP_FROM: str = os.getenv('SMTP_FROM', '')  # defaults to SMTP_USER when sending
    SMTP_USE_TLS: bool = os.getenv('SMTP_USE_TLS', 'true').lower() in ('true', '1', 'yes', 'on')
    SMTP_USE_SSL: bool = os.getenv('SMTP_USE_SSL', 'false').lower() in ('true', '1', 'yes', 'on')
    REPORT_DAILY_HOUR: int = int(os.getenv('REPORT_DAILY_HOUR', '8'))
    REPORT_DAILY_MINUTE: int = int(os.getenv('REPORT_DAILY_MINUTE', '0'))
    REPORT_WEEKLY_DAY: str = os.getenv('REPORT_WEEKLY_DAY', 'mon')  # mon,tue,...
    REPORT_MONTHLY_DAY: int = int(os.getenv('REPORT_MONTHLY_DAY', '1'))  # day of month
    
    @classmethod
    def validate(cls) -> list:
        """Return list of missing required configs."""
        missing = []
        config = cls()
        if not config.TELEGRAM_API_ID:
            missing.append('TELEGRAM_API_ID')
        if not config.TELEGRAM_API_HASH:
            missing.append('TELEGRAM_API_HASH')
        if not config.POCKET_OPTION_EMAIL:
            missing.append('POCKET_OPTION_EMAIL')
        if not config.POCKET_OPTION_PASSWORD:
            missing.append('POCKET_OPTION_PASSWORD')
        return missing
