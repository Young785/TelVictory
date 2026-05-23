"""Multi-platform trading adapter supporting Pocket Option, Deriv, and more."""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class PlatformStatus:
    """Status container for platform connection."""
    def __init__(self):
        self.connected = False
        self.authenticated = False
        self.balance = 0.0
        self.currency = "USD"
        self.account_type = "demo"  # demo or real
        self.simulation_mode = False
        self.error_message = None
        self.last_updated = None
        self.platform_name = "Unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'connected': self.connected,
            'authenticated': self.authenticated,
            'balance': self.balance,
            'currency': self.currency,
            'account_type': self.account_type,
            'simulation_mode': self.simulation_mode,
            'error_message': self.error_message,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'platform_name': self.platform_name
        }


class TradingPlatform(ABC):
    """Abstract base class for trading platforms."""

    def __init__(self, name: str, credentials: Dict[str, str]):
        self.name = name
        self.credentials = credentials
        self.status = PlatformStatus()
        self.status.platform_name = name
        self.api = None
        self._trade_callbacks: list[Callable] = []

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to trading platform."""
        pass

    @abstractmethod
    async def disconnect(self) -> bool:
        """Disconnect from platform."""
        pass

    @abstractmethod
    async def place_trade(self, pair: str, amount: float, direction: str,
                          expiration: int, is_otc: bool = False) -> Dict[str, Any]:
        """Place a trade."""
        pass

    @abstractmethod
    async def get_balance(self) -> float:
        """Get account balance."""
        pass

    def on_trade(self, callback: Callable):
        """Register trade callback."""
        self._trade_callbacks.append(callback)

    def _notify_trade(self, trade_data: Dict):
        """Notify all callbacks of trade update."""
        for callback in self._trade_callbacks:
            try:
                callback(trade_data)
            except Exception as e:
                logger.error(f"Trade callback error: {e}")

    def update_status(self, **kwargs):
        """Update platform status."""
        for key, value in kwargs.items():
            if hasattr(self.status, key):
                setattr(self.status, key, value)
        self.status.last_updated = datetime.now()


class PlatformManager:
    """Manages multiple trading platforms."""

    def __init__(self):
        self.platforms: Dict[str, TradingPlatform] = {}
        self.active_platform: Optional[str] = None
        self._platform_classes: Dict[str, Any] = {}

    def register_platform_class(self, name: str, platform_class):
        """Register a platform class."""
        self._platform_classes[name] = platform_class
        logger.info(f"Registered platform class: {name}")

    def register_platform(self, name: str, platform_type: str, credentials: Dict[str, str]):
        """Register a new platform instance."""
        if platform_type not in self._platform_classes:
            raise ValueError(f"Unknown platform type: {platform_type}. Register it first.")

        platform_class = self._platform_classes[platform_type]
        self.platforms[name] = platform_class(credentials)
        logger.info(f"Registered platform instance: {name} ({platform_type})")

    async def connect_all(self):
        """Connect to all registered platforms."""
        for name, platform in self.platforms.items():
            success = await platform.connect()
            if success and not self.active_platform:
                self.active_platform = name
            logger.info(f"Platform {name}: {'Connected' if success else 'Failed'}")

    async def connect(self, name: str) -> bool:
        """Connect to a specific platform."""
        if name not in self.platforms:
            logger.error(f"Platform {name} not registered")
            return False

        success = await self.platforms[name].connect()
        if success:
            self.active_platform = name
        return success

    async def disconnect(self, name: str) -> bool:
        """Disconnect a specific platform."""
        if name in self.platforms:
            return await self.platforms[name].disconnect()
        return False

    async def disconnect_all(self):
        """Disconnect all platforms."""
        for name, platform in self.platforms.items():
            await platform.disconnect()
        self.active_platform = None

    def get_platform(self, name: Optional[str] = None) -> Optional[TradingPlatform]:
        """Get platform by name or active platform."""
        if name:
            return self.platforms.get(name)
        if self.active_platform:
            return self.platforms.get(self.active_platform)
        return None

    def get_active_platform(self) -> Optional[TradingPlatform]:
        """Get currently active platform."""
        if self.active_platform:
            return self.platforms.get(self.active_platform)
        return None

    def get_all_status(self) -> Dict[str, Dict]:
        """Get status of all platforms."""
        return {name: platform.status.to_dict() for name, platform in self.platforms.items()}

    def get_active_status(self) -> Optional[Dict]:
        """Get status of active platform."""
        platform = self.get_active_platform()
        if platform:
            return platform.status.to_dict()
        return None

    async def place_trade(self, pair: str, amount: float, direction: str,
                          expiration: int, is_otc: bool = False) -> Optional[Dict]:
        """Place trade on active platform."""
        platform = self.get_active_platform()
        if not platform:
            logger.error("No active platform available")
            return None

        return await platform.place_trade(pair, amount, direction, expiration, is_otc)


# Global platform manager instance
platform_manager = PlatformManager()


async def init_platforms_from_env():
    """Initialize platforms from environment variables."""
    from config import Config
    import os

    config = Config()

    # Import and register platform classes
    try:
        from .pocket_option import PocketOptionPlatform
        platform_manager.register_platform_class('pocket_option', PocketOptionPlatform)

        # Initialize Pocket Option if credentials available
        if config.POCKET_OPTION_SSID or config.POCKET_OPTION_EMAIL:
            platform_manager.register_platform(
                'pocket_option',
                'pocket_option',
                {
                    'ssid': config.POCKET_OPTION_SSID,
                    'email': config.POCKET_OPTION_EMAIL,
                    'password': config.POCKET_OPTION_PASSWORD
                }
            )
            await platform_manager.connect('pocket_option')
    except ImportError as e:
        logger.warning(f"Pocket Option platform not available: {e}")

    # Initialize Deriv if credentials available
    try:
        from .deriv import DerivPlatform
        platform_manager.register_platform_class('deriv', DerivPlatform)

        deriv_token = os.getenv('DERIV_API_TOKEN', '')
        if deriv_token:
            platform_manager.register_platform(
                'deriv',
                'deriv',
                {'api_token': deriv_token, 'app_id': os.getenv('DERIV_APP_ID', '12345')}
            )
            await platform_manager.connect('deriv')
    except ImportError as e:
        logger.warning(f"Deriv platform not available: {e}")

    return platform_manager
