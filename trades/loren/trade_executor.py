from abc import ABC, abstractmethod
from typing import Optional, Callable
from datetime import datetime, timedelta
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# Monkey patch logger for BinaryOptionsToolsV2 compatibility with Python 3.13
if not hasattr(logging.Logger, 'warn'):
    logging.Logger.warn = lambda self, msg, *args, **kwargs: self.warning(msg, *args, **kwargs)

try:
    from BinaryOptionsToolsV2 import PocketOptionAsync
    POCKET_API_AVAILABLE = True
except ImportError:
    POCKET_API_AVAILABLE = False

import asyncio
from signal_parser import TradingSignal
from config import Config

def parse_timezone_offset(timezone_str: str) -> int:
    """Parse TIMEZONE config (e.g. UTC+1) to hours offset."""
    if not timezone_str or timezone_str.upper() == 'UTC':
        return 0
    tz_upper = timezone_str.upper()
    if tz_upper.startswith('UTC'):
        sign = 1
        offset_str = tz_upper[3:]
        if offset_str.startswith('+'):
            offset_str = offset_str[1:]
        elif offset_str.startswith('-'):
            sign = -1
            offset_str = offset_str[1:]
        try:
            return sign * int(offset_str)
        except ValueError:
            return 0
    return 0
from database import trades_db, signals_db, log_event
from telegram_notifier import notify_trade
logger = logging.getLogger(__name__)


class TradingPlatform(ABC):
    """Abstract base class for trading platforms."""
    
    @abstractmethod
    async def connect(self) -> bool:
        """Connect to trading platform."""
        pass
    
    @abstractmethod
    async def place_trade(self, pair: str, amount: float, direction: str, 
                          expiration: int, is_otc: bool = False) -> dict:
        """Place a trade."""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """Disconnect from platform."""
        pass


class ManagedPlatformAdapter(TradingPlatform):
    """Use the Web UI's connected platform (platform_manager) for live trades."""

    def __init__(self, managed_platform):
        self._platform = managed_platform
        self.connected = False
        self.simulation_mode = False

    async def connect(self) -> bool:
        if self._platform.status.connected:
            self.connected = True
            self.simulation_mode = self._platform.status.simulation_mode
            return True
        ok = await self._platform.connect()
        self.connected = self._platform.status.connected
        self.simulation_mode = self._platform.status.simulation_mode
        return ok

    async def place_trade(self, pair: str, amount: float, direction: str,
                          expiration: int, is_otc: bool = False) -> dict:
        result = await self._platform.place_trade(pair, amount, direction, expiration, is_otc)
        if result.get('error'):
            return {"status": "error", "error": result['error']}
        status = "simulated" if result.get('is_simulation') else "success"
        return {
            "status": status,
            "id": str(result.get('id', f"trade_{datetime.now().timestamp()}")),
            "pair": result.get('pair', pair),
            "amount": amount,
            "direction": direction.lower() if isinstance(direction, str) else direction,
            "expiration": expiration,
            "api_response": result,
            "result": result.get('result', 'pending'),
        }

    async def disconnect(self):
        # Keep shared platform connection alive for the Web UI
        self.connected = False


class PocketOptionPlatform(TradingPlatform):
    """Pocket Option trading platform using BinaryOptionsToolsV2."""
    
    def __init__(self, email: str, password: str, ssid: str = ''):
        self.email = email
        self.password = password
        self.ssid = ssid
        self.connected = False
        self.api = None
        self.simulation_mode = False
    
    async def connect(self) -> bool:
        try:
            if POCKET_API_AVAILABLE and self.ssid:
                # New V2 API - just use SSID
                self.api = PocketOptionAsync(ssid=self.ssid)
                await self.api.connect()
                balance = await self.api.balance()
                logger.info(f"Connected to Pocket Option - Balance: ${balance}")
                self.simulation_mode = False
            else:
                if not self.ssid:
                    logger.warning("No POCKET_OPTION_SSID provided - running in simulation mode")
                else:
                    logger.warning("BinaryOptionsToolsV2 not installed - running in simulation mode")
                self.simulation_mode = True
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Pocket Option: {e}")
            logger.warning("⚠️  Switching to SIMULATION mode - no real trades will be placed")
            logger.info("To fix this, get your SSID from Pocket Option browser DevTools:")
            logger.info("  1. Open pocketoption.com in browser and login")
            logger.info("  2. Press F12 → Network tab → WS filter")
            logger.info("  3. Look for auth message: 42[\"auth\",{\"session\":\"...\",...}]")
            logger.info("  4. Copy the session token and add to .env:")
            logger.info("     POCKET_OPTION_SSID=your_session_token_here")
            self.connected = True
            self.simulation_mode = True
            return True
    
    async def place_trade(self, pair: str, amount: float, direction: str,
                          expiration: int, is_otc: bool = False) -> dict:
        if not self.connected:
            await self.connect()

        # Format pair for BinaryOptionsToolsV2 (uppercase, no slashes)
        base_pair = pair.replace('/', '').upper()

        is_buy = direction == "BUY"
        action = "buy" if is_buy else "sell"

        logger.info(f"Placing trade: {action} {base_pair} ${amount} exp {expiration}M")

        if POCKET_API_AVAILABLE and self.api and not self.simulation_mode:
            duration_seconds = expiration * 60

            async def try_trade(asset: str):
                """Helper to execute trade."""
                if is_buy:
                    return await self.api.buy(asset, amount, duration_seconds)
                else:
                    return await self.api.sell(asset, amount, duration_seconds)

            # Lorenzo/OTC channels use *_otc assets — try OTC first when flagged
            assets_to_try = [base_pair + "_otc", base_pair] if is_otc else [base_pair, base_pair + "_otc"]
            last_error = None

            for asset in assets_to_try:
                try:
                    trade_id, deal = await try_trade(asset)
                    trade_record = trades_db.insert({
                        'pair': asset,
                        'direction': action,
                        'amount': amount,
                        'expiration': expiration,
                        'trade_id': str(trade_id) if trade_id else None,
                        'result': 'pending',
                        'profit': 0,
                        'type': 'live'
                    })
                    log_event("info", f"Trade placed: {asset} {action} ${amount}", "trade_executor")

                    return {
                        "status": "success" if trade_id else "failed",
                        "id": str(trade_id) if trade_id else f"trade_{datetime.now().timestamp()}",
                        "pair": asset,
                        "amount": amount,
                        "direction": action,
                        "expiration": expiration,
                        "api_response": deal,
                        "db_id": trade_record['id'],
                        "note": "OTC asset" if asset.endswith("_otc") else None
                    }
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"Trade failed for {asset}: {e}")

            logger.error(f"API trade failed for all assets: {last_error}")
            return {"status": "error", "error": last_error or "Unknown error"}
        else:
            # Simulation mode
            logger.info(f"[SIMULATION] Would place trade: {action} {base_pair} ${amount}")
            return {
                "status": "simulated",
                "id": f"simulated_{datetime.now().timestamp()}",
                "pair": base_pair,
                "amount": amount,
                "direction": action,
                "expiration": expiration
            }
    
    async def disconnect(self):
        if self.api and not self.simulation_mode:
            try:
                await self.api.close()
            except:
                pass
        self.connected = False
        logger.info("Disconnected from Pocket Option")


class TradeExecutor:
    """Manages scheduled trade execution with martingale."""
    
    def __init__(self, config: Config, platform: Optional[TradingPlatform] = None):
        self.config = config
        self.platform = platform or PocketOptionPlatform(
            config.POCKET_OPTION_EMAIL,
            config.POCKET_OPTION_PASSWORD,
            config.POCKET_OPTION_SSID
        )
        # Initialize scheduler with proper event loop handling
        self.scheduler = None
        self.active_trades: dict = {}
    
    async def start(self):
        # Create scheduler with the current running event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        self.scheduler = AsyncIOScheduler(event_loop=loop)
        self.scheduler.start()

        # Prefer Web UI platform connection (Connect button) over a separate session
        used_managed = False
        try:
            from platforms import platform_manager
            active = platform_manager.get_active_platform()
            if active:
                adapter = ManagedPlatformAdapter(active)
                if await adapter.connect():
                    self.platform = adapter
                    used_managed = True
                    mode = "simulation" if adapter.simulation_mode else "live"
                    logger.info(f"Trade executor using platform manager ({active.name}, {mode})")
        except Exception as e:
            logger.debug(f"Platform manager unavailable, using built-in platform: {e}")

        if not used_managed:
            await self.platform.connect()

        await self.reload_pending_signals()
        logger.info("Trade executor started with scheduler")

    async def reload_pending_signals(self):
        """Re-schedule signals saved while bot was offline."""
        from database import signals_db

        now = datetime.now()
        reloaded = 0
        for rec in signals_db.get_all():
            if rec.get('status') != 'scheduled':
                continue
            try:
                entry_raw = rec.get('entry_execute_at')
                if entry_raw:
                    entry_dt = datetime.fromisoformat(entry_raw)
                else:
                    sig = self._signal_from_record(rec)
                    entry_dt = sig.entry_datetime if sig else None
                if not entry_dt:
                    continue
                diff = (entry_dt - now).total_seconds() / 60
                if diff < -30:
                    continue
                if entry_dt.date() < now.date() and diff < -60:
                    continue
                sig = self._signal_from_record(rec)
                if not sig:
                    continue
                if diff <= 2:
                    logger.info(f"🔄 Running missed signal immediately: {sig.pair} {sig.direction}")
                    await self.execute_trade_now(sig, 0, self.config.DEFAULT_AMOUNT)
                    if sig.martingale_levels:
                        self.schedule_martingale_only(sig)
                else:
                    logger.info(f"🔄 Re-scheduling signal: {sig.pair} at {entry_dt.strftime('%H:%M')}")
                    self.schedule_signal(sig)
                reloaded += 1
            except Exception as e:
                logger.error(f"Failed to reload signal {rec.get('id')}: {e}")
        if reloaded:
            log_event("info", f"🔄 Reloaded {reloaded} pending signal(s) from database", "trade_executor")

    def _signal_from_record(self, rec: dict) -> Optional[TradingSignal]:
        """Rebuild TradingSignal from a database record."""
        try:
            received = rec.get('created_at')
            received_at = datetime.fromisoformat(received) if received else datetime.now()
            return TradingSignal(
                pair=rec['pair'],
                expiration_minutes=int(rec.get('expiration_minutes', 5)),
                entry_time=rec['entry_time'],
                direction=rec['direction'],
                martingale_levels=rec.get('martingale_levels') or [],
                is_otc=bool(rec.get('is_otc', False)),
                received_at=received_at,
                timezone_offset=parse_timezone_offset(self.config.TIMEZONE),
            )
        except Exception as e:
            logger.error(f"Invalid signal record: {e}")
            return None

    async def stop(self):
        if self.scheduler:
            self.scheduler.shutdown()
        await self.platform.disconnect()
        logger.info("Trade executor stopped")
    
    def schedule_signal(self, signal: TradingSignal):
        """Schedule all trades for a signal including martingale levels."""
        if not self.scheduler:
            logger.error("Scheduler not initialized, cannot schedule trade")
            return
            
        entry_time = signal.entry_datetime
        if not entry_time:
            logger.error(f"Invalid entry time: {signal.entry_time}")
            return
        
        # Schedule main entry
        self.scheduler.add_job(
            self._execute_trade,
            trigger=DateTrigger(run_date=entry_time),
            args=[signal, 0, self.config.DEFAULT_AMOUNT],
            id=f"trade_{signal.pair}_{signal.entry_time}_0",
            replace_existing=True
        )
        logger.info(f"✅ SCHEDULED: Main trade {signal.pair} {signal.direction} at {entry_time.strftime('%H:%M:%S')}")
        
        # Schedule martingale levels using same logic as entry_datetime
        for i, mg_time_str in enumerate(signal.martingale_levels[:self.config.MAX_MARTINGALE_LEVELS]):
            try:
                hour, minute = map(int, mg_time_str.split(':'))
                # Build martingale time using same timezone logic as entry time
                mg_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
                # Apply same timezone offset correction as entry time (convert signal time to local)
                mg_time = mg_time - timedelta(hours=signal.timezone_offset)
                
                # Smart scheduling logic matching entry_datetime logic
                mg_time_diff_minutes = (mg_time - datetime.now()).total_seconds() / 60
                
                if mg_time < datetime.now():
                    minutes_ago = -mg_time_diff_minutes
                    if minutes_ago > 5 and minutes_ago <= 120:
                        # Was 5-120 minutes ago, likely for tomorrow
                        mg_time = mg_time + timedelta(days=1)
                    elif minutes_ago > 120:
                        # Was > 2 hours ago, definitely for tomorrow
                        mg_time = mg_time + timedelta(days=1)
                
                # Ensure martingale is after entry time (if not, add a day)
                if mg_time <= entry_time:
                    mg_time = mg_time + timedelta(days=1)
                    logger.info(f"Martingale level {i+1} adjusted to be after entry time: {mg_time}")

                amount = self.config.DEFAULT_AMOUNT * (self.config.MARTINGALE_MULTIPLIER ** (i + 1))

                self.scheduler.add_job(
                    self._execute_trade,
                    trigger=DateTrigger(run_date=mg_time),
                    args=[signal, i + 1, amount],
                    id=f"trade_{signal.pair}_{signal.entry_time}_{i+1}",
                    replace_existing=True
                )
                logger.info(f"✅ SCHEDULED: Martingale level {i+1} {signal.pair} {signal.direction} ${amount:.2f} at {mg_time.strftime('%H:%M:%S')}")
            except Exception as e:
                logger.error(f"Failed to schedule martingale level {i+1}: {e}")

    async def execute_trade_now(self, signal: TradingSignal, level: int, amount: float):
        """Execute a trade immediately (not scheduled)."""
        await self._execute_trade(signal, level, amount)

    def schedule_martingale_only(self, signal: TradingSignal):
        """Schedule only martingale levels (for when main trade executes immediately)."""
        if not self.scheduler:
            logger.error("Scheduler not initialized, cannot schedule martingale")
            return
            
        entry_time = signal.entry_datetime
        if not entry_time:
            return

        for i, mg_time_str in enumerate(signal.martingale_levels[:self.config.MAX_MARTINGALE_LEVELS]):
            try:
                hour, minute = map(int, mg_time_str.split(':'))
                mg_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
                mg_time = mg_time - timedelta(hours=signal.timezone_offset)
                
                # Smart scheduling logic matching entry_datetime logic
                mg_time_diff_minutes = (mg_time - datetime.now()).total_seconds() / 60
                
                if mg_time < datetime.now():
                    minutes_ago = -mg_time_diff_minutes
                    if minutes_ago > 5 and minutes_ago <= 120:
                        # Was 5-120 minutes ago, likely for tomorrow
                        mg_time = mg_time + timedelta(days=1)
                    elif minutes_ago > 120:
                        # Was > 2 hours ago, definitely for tomorrow
                        mg_time = mg_time + timedelta(days=1)
                
                # Ensure martingale is after entry time (if not, add a day)
                if mg_time <= entry_time:
                    mg_time = mg_time + timedelta(days=1)

                amount = self.config.DEFAULT_AMOUNT * (self.config.MARTINGALE_MULTIPLIER ** (i + 1))

                self.scheduler.add_job(
                    self._execute_trade,
                    trigger=DateTrigger(run_date=mg_time),
                    args=[signal, i + 1, amount],
                    id=f"trade_{signal.pair}_{signal.entry_time}_{i+1}",
                    replace_existing=True
                )
                logger.info(f"✅ SCHEDULED: Martingale level {i+1} {signal.pair} {signal.direction} ${amount:.2f} at {mg_time.strftime('%H:%M:%S')}")
            except Exception as e:
                logger.error(f"Failed to schedule martingale level {i+1}: {e}")

    async def _resolve_platform(self, signal: TradingSignal) -> TradingPlatform:
        """Use channel-specific platform when connected."""
        pid = getattr(signal, 'platform_id', None) or 'pocket_option'
        if pid in ('none', '', 'mt5'):
            return self.platform
        try:
            from platforms import platform_manager
            p = platform_manager.get_platform(pid)
            if p and p.status.connected:
                adapter = ManagedPlatformAdapter(p)
                adapter.connected = True
                adapter.simulation_mode = p.status.simulation_mode
                return adapter
        except Exception as e:
            logger.debug(f"Channel platform {pid} unavailable: {e}")
        return self.platform

    async def _execute_trade(self, signal: TradingSignal, level: int, amount: float):
        """Execute a single trade."""
        level_name = "MAIN" if level == 0 else f"MG{level}"
        platform = await self._resolve_platform(signal)
        pid = getattr(signal, 'platform_id', 'pocket_option')
        logger.info(f"🚀 EXECUTING TRADE: {signal.pair} {signal.direction} ${amount:.2f} [{level_name}] via {pid}")

        if not hasattr(platform, 'connected') or not platform.connected:
            logger.error("Cannot execute trade: Platform not connected")
            log_event("error", f"Trade skipped — {pid} not connected: {signal.pair}", "trade_executor")
            return

        try:
            result = await platform.place_trade(
                pair=signal.pair,
                amount=amount,
                direction=signal.direction,
                expiration=signal.expiration_minutes,
                is_otc=signal.is_otc
            )
            
            trade_record = {
                'pair': signal.pair,
                'direction': signal.direction,
                'amount': amount,
                'expiration': signal.expiration_minutes,
                'is_otc': signal.is_otc,
                'level': level,
                'status': 'executed',
                'platform_result': result,
                'executed_at': datetime.now().isoformat(),
                'signal_received_at': signal.received_at.isoformat() if signal.received_at else None
            }
            
            # Save to database
            trades_db.insert(trade_record)
            logger.info(f"✅ Trade saved to database: {signal.pair} {signal.direction} ${amount}")
            
            self.active_trades[result.get('id')] = {
                'signal': signal,
                'level': level,
                'amount': amount,
                'result': result,
                'executed_at': datetime.now()
            }
            
            trade_status = result.get('status', 'unknown')
            trade_result = result.get('result', 'pending')
            log_event(
                "info",
                f"🚀 TRADE EXECUTED: {signal.pair} {signal.direction} ${amount} [{level_name}] - {trade_status}",
                "trade_executor"
            )
            logger.info(f"✅ Trade executed successfully: {result}")
            
            # Send Telegram notification for trade placement
            trade_dict = {
                'pair': signal.pair,
                'direction': signal.direction,
                'amount': amount,
                'platform': pid,
                'result': trade_result if trade_result else 'pending',
                'profit': 0
            }
            notify_trade(trade_dict, 'PLACED')
            
        except Exception as e:
            logger.error(f"❌ Trade execution failed: {e}")
            # Save failed trade to database
            trades_db.insert({
                'pair': signal.pair,
                'direction': signal.direction,
                'amount': amount,
                'expiration': signal.expiration_minutes,
                'is_otc': signal.is_otc,
                'level': level,
                'status': 'failed',
                'error': str(e),
                'executed_at': datetime.now().isoformat()
            })
            
            # Send error notification
            from telegram_notifier import get_notifier
            notifier = get_notifier()
            try:
                asyncio.create_task(notifier.notify_error(str(e), f"Trade {signal.pair} {signal.direction}"))
            except:
                pass
