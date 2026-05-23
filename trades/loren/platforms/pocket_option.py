"""Pocket Option trading platform implementation."""

import logging
from datetime import datetime
from typing import Dict, Any

from . import TradingPlatform

logger = logging.getLogger(__name__)

# Platform availability flag
try:
    from BinaryOptionsToolsV2 import PocketOptionAsync
    POCKET_API_AVAILABLE = True
except ImportError:
    POCKET_API_AVAILABLE = False
    logging.warning("BinaryOptionsToolsV2 not installed - Pocket Option will use simulation mode")


class PocketOptionPlatform(TradingPlatform):
    """Pocket Option trading platform."""

    def __init__(self, credentials: Dict[str, str]):
        super().__init__("Pocket Option", credentials)
        self.ssid = credentials.get('ssid', '')
        self.email = credentials.get('email', '')

    async def connect(self) -> bool:
        """Connect to Pocket Option."""
        try:
            if POCKET_API_AVAILABLE and self.ssid:
                logger.info("🔌 Connecting to Pocket Option...")
                self.api = PocketOptionAsync(ssid=self.ssid)
                
                # Add timeout wrapper for the connection
                import asyncio
                try:
                    await asyncio.wait_for(self.api.connect(), timeout=30.0)
                except asyncio.TimeoutError:
                    raise Exception("Connection timed out after 30 seconds - Pocket Option servers may be slow or SSID is invalid")
                
                balance = await self.api.balance()

                self.update_status(
                    connected=True,
                    authenticated=True,
                    balance=float(balance),
                    simulation_mode=False,
                    error_message=None
                )
                logger.info(f"✅ Connected to Pocket Option - Balance: ${balance}")
                return True
            else:
                # Simulation mode
                reason = "BinaryOptionsToolsV2 not installed" if not POCKET_API_AVAILABLE else "POCKET_OPTION_SSID not configured"
                self.update_status(
                    connected=True,
                    authenticated=False,
                    balance=10000.0,  # Demo balance
                    simulation_mode=True,
                    error_message=f"Running in simulation mode - {reason}"
                )
                logger.warning(f"⚠️  Pocket Option running in SIMULATION mode ({reason})")
                logger.warning("   No real trades will be placed - configure SSID in .env for live trading")
                return True

        except Exception as e:
            error_str = str(e).lower()
            is_timeout = "timeout" in error_str or "timed out" in error_str
            is_auth_error = "auth" in error_str or "unauthorized" in error_str or "invalid" in error_str
            
            self.update_status(
                connected=False,
                authenticated=False,
                error_message=str(e)
            )
            
            logger.error("="*60)
            logger.error(f"❌ Failed to connect to Pocket Option: {e}")
            logger.error("")
            
            if is_timeout:
                logger.error("🔧 CAUSE: Connection timed out")
                logger.error("   - Pocket Option servers may be experiencing issues")
                logger.error("   - Your internet connection may be unstable")
                logger.error("   - The SSID token may be invalid/expired")
            elif is_auth_error:
                logger.error("🔧 CAUSE: Authentication failed")
                logger.error("   - Your SSID token is invalid or expired")
            else:
                logger.error(f"🔧 CAUSE: {e}")
                
            logger.error("")
            logger.error("💡 SOLUTIONS:")
            logger.error("   1. Get a fresh SSID from Pocket Option:")
            logger.error("      - Open pocketoption.com in browser and login")
            logger.error("      - Press F12 → Network tab → Filter: WS")
            logger.error("      - Look for auth message with your session token")
            logger.error("   2. Update .env file: POCKET_OPTION_SSID=your_token")
            logger.error("   3. Or run in simulation mode (trades will be logged but not placed)")
            logger.error("="*60)
            
            # Fall back to simulation mode so bot can still run
            logger.warning("🔄 Falling back to SIMULATION mode - trades will be logged but not placed")
            self.api = None
            self.update_status(
                connected=True,
                authenticated=False,
                balance=10000.0,
                simulation_mode=True,
                error_message=f"Connection failed: {str(e)[:50]}... Running in simulation"
            )
            return True  # Return True so bot continues running in sim mode

    async def disconnect(self) -> bool:
        """Disconnect from Pocket Option."""
        if self.api:
            try:
                await self.api.close()
            except:
                pass
        self.update_status(connected=False, authenticated=False)
        logger.info("Disconnected from Pocket Option")
        return True

    async def place_trade(self, pair: str, amount: float, direction: str,
                          expiration: int, is_otc: bool = False) -> Dict[str, Any]:
        """Place a trade on Pocket Option."""
        # Format asset for BinaryOptionsToolsV2 (uppercase, no slashes)
        base_pair = pair.replace('/', '').upper()
        formatted_pair = base_pair

        # Convert expiration from minutes to seconds
        expiration_seconds = expiration * 60

        is_buy = direction.upper() == "BUY"
        action = "buy" if is_buy else "sell"

        logger.info(f"Placing trade: {action} {formatted_pair} ${amount} exp {expiration}M ({expiration_seconds}s)")

        if self.status.simulation_mode:
            # Simulation mode - return mock result
            import random
            result = random.choice(['win', 'loss'])
            profit = amount * 0.92 if result == 'win' else -amount

            trade_data = {
                'id': f'sim_{datetime.now().timestamp()}',
                'pair': formatted_pair,
                'direction': direction,
                'amount': amount,
                'expiration': expiration,
                'result': result,
                'profit': profit,
                'is_simulation': True,
                'timestamp': datetime.now().isoformat()
            }
            self._notify_trade(trade_data)
            return trade_data

        async def execute_trade(asset: str):
            """Helper to execute trade with given asset."""
            if is_buy:
                return await self.api.buy(
                    asset=asset,
                    amount=amount,
                    time=expiration_seconds,
                    check_win=False
                )
            else:
                return await self.api.sell(
                    asset=asset,
                    amount=amount,
                    time=expiration_seconds,
                    check_win=False
                )

        assets_to_try = [base_pair + "_otc", base_pair] if is_otc else [base_pair, base_pair + "_otc"]
        last_error = None

        for asset in assets_to_try:
            try:
                trade_id, trade_details = await execute_trade(asset)
                trade_data = {
                    'id': str(trade_id),
                    'pair': asset,
                    'direction': direction,
                    'amount': amount,
                    'expiration': expiration,
                    'result': trade_details.get('result', 'pending') if isinstance(trade_details, dict) else 'pending',
                    'profit': trade_details.get('profit', 0) if isinstance(trade_details, dict) else 0,
                    'is_simulation': False,
                    'timestamp': datetime.now().isoformat(),
                    'details': trade_details,
                    'note': 'OTC asset' if asset.endswith('_otc') else None
                }
                self._notify_trade(trade_data)
                logger.info(f"✅ Trade placed successfully: {trade_id} on {asset}")
                return trade_data
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Trade failed for {asset}: {e}")

        logger.error(f"Trade failed for all assets: {last_error}")
        return {
            'error': last_error or 'Unknown error',
            'pair': base_pair,
            'amount': amount,
            'timestamp': datetime.now().isoformat()
        }

    async def get_balance(self) -> float:
        """Get Pocket Option balance."""
        if self.api and not self.status.simulation_mode:
            try:
                balance = await self.api.balance()
                self.status.balance = float(balance)
                return self.status.balance
            except Exception as e:
                logger.error(f"Failed to get balance: {e}")
        return self.status.balance
