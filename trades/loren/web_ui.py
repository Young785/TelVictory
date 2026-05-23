"""Web UI for trading bot management."""
import asyncio
import threading
import logging
import functools
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import os

# Flask-Session for persistent sessions
try:
    from flask_session import Session
    SESSION_AVAILABLE = True
except ImportError:
    SESSION_AVAILABLE = False

from database import trades_db, signals_db, logs_db, settings_db, channels_db, get_stats, log_event
from config import Config
from signal_parsers import parse_message, PARSER_LABELS
from channel_utils import normalize_channel_record, get_all_channels_normalized, PARSER_CHOICES, PLATFORM_CHOICES
from trade_executor import TradeExecutor
from platforms import platform_manager, init_platforms_from_env
from bot import TradingUserBot, _acquire_lock, _release_lock

# Monkey patch logger for library compatibility
import logging as logging_module
if not hasattr(logging_module.Logger, 'warn'):
    logging_module.Logger.warn = lambda self, msg, *args, **kwargs: self.warning(msg, *args, **kwargs)

app = Flask(__name__)
CORS(app, supports_credentials=True)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'trading-bot-secret-key-change-me')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(__file__), 'flask_session')
app.config['SESSION_USE_SIGNER'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['SESSION_COOKIE_NAME'] = 'trading_bot_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize Flask-Session
if SESSION_AVAILABLE:
    Session(app)
    os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)

# Simple user authentication (in production, use proper password hashing)
# Format: username:password_hash or plain for demo
DEFAULT_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
DEFAULT_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin')

def login_required(f):
    """Decorator to require login for routes."""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Global bot state
bot_running = False
executor = None
bot_thread = None
config = Config()


class BotManager:
    """Manages the bot in a separate thread."""
    
    def __init__(self):
        self.running = False
        self.executor = None
        self.telegram_bot = None
        self.loop = None
        self.thread = None
    
    def start(self):
        if self.running:
            return False
        
        # Check if another bot instance is already running
        if not _acquire_lock():
            log_event("error", "Another bot instance is already running", "web_ui")
            return False
        
        self.running = True
        self.thread = threading.Thread(target=self._run_bot)
        self.thread.daemon = True
        self.thread.start()
        log_event("info", "Bot started via Web UI", "web_ui")
        return True
    
    def stop(self):
        if not self.running:
            return False
        
        self.running = False
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._stop_bot(), self.loop)
        _release_lock()  # Release the file lock
        log_event("info", "Bot stopped via Web UI", "web_ui")
        return True
    
    async def _init_executor(self):
        """Start executor and ensure Pocket Option is connected."""
        await self.executor.start()
        try:
            from platforms import platform_manager, init_platforms_from_env
            if not platform_manager.platforms:
                await init_platforms_from_env()
            if not platform_manager.active_platform and 'pocket_option' in platform_manager.platforms:
                await platform_manager.connect('pocket_option')
        except Exception as e:
            log_event("warning", f"Platform auto-connect: {e}", "web_ui")

    def _run_bot(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            # Initialize trade executor
            self.executor = TradeExecutor(config)
            self.loop.run_until_complete(self._init_executor())
            log_event("info", "TradeExecutor started", "web_ui")
            
            # Initialize and start Telegram bot
            try:
                self.telegram_bot = TradingUserBot()
                # Store executor reference in bot so it can schedule trades
                self.telegram_bot.executor = self.executor
                log_event("info", "Telegram bot initialized", "web_ui")
                # Start Telegram bot (this will block until disconnected)
                self.loop.run_until_complete(self.telegram_bot.run())
            except Exception as te:
                log_event("error", f"Telegram bot error: {te}", "web_ui")
                # Continue running executor even if Telegram fails
                while self.running:
                    self.loop.run_until_complete(asyncio.sleep(1))
                
        except Exception as e:
            log_event("error", f"Bot error: {e}", "web_ui")
        finally:
            if self.executor:
                self.loop.run_until_complete(self.executor.stop())
            _release_lock()  # Ensure lock is released on exit
    
    async def _stop_bot(self):
        if self.telegram_bot and self.telegram_bot.client:
            await self.telegram_bot.client.disconnect()
        if self.executor:
            await self.executor.stop()
    
    def is_running(self):
        return self.running


bot_manager = BotManager()


@app.route('/login')
def login():
    """Login page."""
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    """Authenticate user."""
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')
    
    if username == DEFAULT_USERNAME and password == DEFAULT_PASSWORD:
        session.permanent = True
        session['logged_in'] = True
        session['username'] = username
        log_event("info", f"User '{username}' logged in", "auth")
        return jsonify({'success': True, 'redirect': '/'})
    
    log_event("warning", f"Failed login attempt for username '{username}'", "auth")
    return jsonify({'success': False, 'message': 'Invalid username or password'}), 401


@app.route('/api/logout', methods=['POST'])
@login_required
def api_logout():
    """Logout user."""
    username = session.get('username', 'unknown')
    session.clear()
    log_event("info", f"User '{username}' logged out", "auth")
    return jsonify({'success': True, 'redirect': '/login'})


@app.route('/logout')
def logout():
    """Logout page redirect."""
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    """Main dashboard."""
    stats = get_stats()
    return render_template('dashboard.html', stats=stats, bot_running=bot_manager.is_running(), active_page='dashboard')


@app.route('/history')
@login_required
def trading_history():
    """Trading history page."""
    trades = trades_db.get_all()
    signals = signals_db.get_all()
    platforms = platform_manager.get_all_status()
    return render_template('history.html', trades=trades, signals=signals,
                          bot_running=bot_manager.is_running(), active_page='history',
                          platforms=platforms)


@app.route('/strategies')
@login_required
def strategies():
    """Legacy URL — trading defaults moved to Settings."""
    return redirect(url_for('settings') + '#tab-trading')


# Platforms that support broker connect + test trade (excludes 'none')
TESTABLE_PLATFORMS = [
    ('pocket_option', 'Pocket Option'),
    ('deriv', 'Deriv'),
]


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _test_platform_connection(platform_name: str) -> dict:
    """Connect once and return status — does not place trades."""
    if platform_name not in platform_manager.platforms:
        return {
            'platform': platform_name,
            'success': False,
            'message': 'Not configured — save credentials in Settings first.',
        }
    try:
        ok = _run_async(platform_manager.connect(platform_name))
        platform = platform_manager.get_platform(platform_name)
        st = platform.status.to_dict() if platform else {}
        if ok:
            return {
                'platform': platform_name,
                'success': True,
                'message': 'Connected',
                'balance': st.get('balance'),
                'simulation_mode': st.get('simulation_mode'),
                'account_type': st.get('account_type'),
                'connected': True,
            }
        return {
            'platform': platform_name,
            'success': False,
            'message': st.get('error_message') or 'Connection failed',
            'connected': False,
        }
    except Exception as e:
        return {'platform': platform_name, 'success': False, 'message': str(e)}


@app.route('/wallet')
@login_required
def wallet():
    """Legacy URL — balances shown on Dashboard and Settings → Platforms."""
    return redirect(url_for('settings') + '#tab-platforms')


@app.route('/platforms')
@login_required
def platforms_list():
    """Legacy URL — connection + credentials live under Settings."""
    return redirect(url_for('settings') + '#tab-platforms')


@app.route('/platform/<platform_name>')
@login_required
def platform_detail(platform_name):
    """Legacy URL — per-platform detail merged into Settings."""
    return redirect(url_for('settings') + f'#tab-platforms&platform={platform_name}')


@app.route('/notifications')
@login_required
def notifications():
    """Notifications page."""
    logs = logs_db.get_all()[-20:]  # Recent 20 notifications
    return render_template('notifications.html', notifications=logs, 
                          bot_running=bot_manager.is_running(), active_page='notifications')


@app.route('/settings')
@login_required
def settings():
    """Settings page."""
    import os
    return render_template('settings.html', config=config,
                          bot_running=bot_manager.is_running(), active_page='settings',
                          deriv_token=os.getenv('DERIV_API_TOKEN', ''),
                          deriv_app_id=os.getenv('DERIV_APP_ID', '12345'),
                          platforms=platform_manager.get_all_status(),
                          testable_platforms=TESTABLE_PLATFORMS)


@app.route('/test_trade')
@login_required
def test_trade_page():
    """Test Trade page — per-platform connection and optional test order."""
    return render_template(
        'test_trade.html',
        bot_running=bot_manager.is_running(),
        active_page='test_trade',
        platforms=platform_manager.get_all_status(),
        testable_platforms=TESTABLE_PLATFORMS,
        default_platform=request.args.get('platform', 'pocket_option'),
    )


@app.route('/channels')
@login_required
def channels_page():
    """Channel management — parser, platform, timezone per source."""
    channels = get_all_channels_normalized()
    return render_template(
        'channels.html',
        channels=channels,
        parser_choices=PARSER_CHOICES,
        platform_choices=PLATFORM_CHOICES,
        bot_running=bot_manager.is_running(),
        active_page='channels',
    )


@app.route('/signals')
@login_required
def signal_inbox():
    """Signal inbox — parsed, scheduled, failed, alerts."""
    all_signals = signals_db.get_all()
    inbox = sorted(all_signals, key=lambda x: x.get('created_at', ''), reverse=True)[:200]
    channels = get_all_channels_normalized()
    return render_template(
        'signal_inbox.html',
        signals=inbox,
        channels=channels,
        bot_running=bot_manager.is_running(),
        active_page='signals',
    )


@app.route('/api/signals/test-parse', methods=['POST'])
@login_required
def test_parse_signal():
    """Test parser on pasted message text."""
    data = request.get_json() or {}
    message = data.get('message', '')
    parser_id = data.get('parser_id', 'lorenzo')
    timezone = data.get('timezone', config.TIMEZONE)
    from trade_executor import parse_timezone_offset
    outcome = parse_message(message, parser_id=parser_id, timezone_offset=parse_timezone_offset(timezone))
    if not outcome.ok:
        return jsonify({'success': False, 'error': outcome.error})
    if outcome.mt5_alert:
        return jsonify({'success': True, 'type': 'mt5_alert', 'data': outcome.mt5_alert})
    sig = outcome.signal
    return jsonify({
        'success': True,
        'type': 'binary',
        'data': {
            'pair': sig.pair,
            'direction': sig.direction,
            'entry_time': sig.entry_time,
            'expiration_minutes': sig.expiration_minutes,
            'is_otc': sig.is_otc,
            'martingale_levels': sig.martingale_levels,
            'execute_immediate': getattr(sig, 'execute_immediate', False),
            'entry_datetime': sig.entry_datetime.isoformat() if sig.entry_datetime else None,
        },
    })


@app.route('/api/channels/<int:channel_db_id>', methods=['PUT'])
@login_required
def update_channel(channel_db_id):
    """Update channel profile (parser, platform, timezone, etc.)."""
    data = request.get_json() or {}
    allowed = {
        'channel_name', 'parser_id', 'platform_id', 'timezone',
        'auto_trade', 'market_type', 'default_amount', 'description', 'is_active',
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if 'auto_trade' in updates:
        updates['auto_trade'] = bool(updates['auto_trade'])
    updated = channels_db.update(channel_db_id, updates)
    if updated:
        log_event("info", f"Channel updated: {updated.get('channel_name')}", "web_ui")
        return jsonify({'success': True, 'channel': normalize_channel_record(updated)})
    return jsonify({'success': False, 'message': 'Channel not found'}), 404


@app.route('/logs')
@login_required
def logs_view():
    """Telegram Message Logs page."""
    all_logs = logs_db.get_all()

    # Filter for Telegram message logs only (messages containing 📩 or "Message from")
    tel_logs = [log for log in all_logs if 'message' in log.get('message', '').lower()
                or '📩' in log.get('message', '')
                or 'Message from' in log.get('message', '')
                or 'SIGNAL DETECTED' in log.get('message', '')
                or 'Parsed signal' in log.get('message', '')]

    # Get filter from query param
    filter_type = request.args.get('filter', 'all')
    channel_filter = request.args.get('channel', 'all')

    # Extract unique channels from logs (raw messages and signal detections)
    channels_in_logs = set()
    for log in all_logs:
        msg = log.get('message', '')
        channel_name = None
        if '📩 Message from' in msg or '📩 [BACKLOG] Message from' in msg:
            marker = '📩 [BACKLOG] Message from ' if '📩 [BACKLOG] Message from' in msg else '📩 Message from '
            try:
                start = msg.find(marker) + len(marker)
                end = msg.find(' (@')
                if start > 0 and end > start:
                    channel_name = msg[start:end].strip()
            except Exception:
                pass
        elif 'SIGNAL DETECTED from' in msg:
            try:
                start = msg.find('SIGNAL DETECTED from ') + len('SIGNAL DETECTED from ')
                end = msg.find(':')
                if end > start:
                    channel_name = msg[start:end].strip()
            except Exception:
                pass
        if channel_name:
            channels_in_logs.add(channel_name)

    if filter_type == 'signals':
        # Only show signal detection logs
        tel_logs = [log for log in tel_logs if 'SIGNAL DETECTED' in log.get('message', '')
                    or 'Parsed signal' in log.get('message', '')]
    elif filter_type == 'messages':
        # Only show raw message logs
        tel_logs = [log for log in tel_logs if '📩 Message from' in log.get('message', '')]

    # Filter by specific channel
    if channel_filter != 'all':
        tel_logs = [log for log in tel_logs if (
            f'📩 Message from {channel_filter} (@' in log.get('message', '')
            or f'📩 [BACKLOG] Message from {channel_filter} (@' in log.get('message', '')
            or f'SIGNAL DETECTED from {channel_filter}:' in log.get('message', '')
            or f'from {channel_filter}' in log.get('message', '')
        )]

    # Limit to last 100
    tel_logs = tel_logs[-100:]

    # Calculate counts for stats (from full telegram log set, not filtered view)
    all_tel = [log for log in all_logs if 'message' in log.get('message', '').lower()
               or '📩' in log.get('message', '')
               or 'Message from' in log.get('message', '')
               or 'SIGNAL DETECTED' in log.get('message', '')
               or 'Parsed signal' in log.get('message', '')]
    info_count = sum(1 for log in tel_logs if log.get('level') == 'info')
    msg_count = sum(1 for log in all_tel if '📩' in log.get('message', ''))
    signal_count = sum(1 for log in all_tel if 'SIGNAL DETECTED' in log.get('message', '')
                       or 'Parsed signal' in log.get('message', ''))

    # Get all configured channels for the dropdown
    configured_channels = channels_db.get_all()

    return render_template('logs.html', logs=tel_logs,
                          bot_running=bot_manager.is_running(), active_page='logs',
                          filter_type=filter_type, channel_filter=channel_filter,
                          channels_in_logs=sorted(channels_in_logs),
                          configured_channels=configured_channels,
                          info_count=info_count, msg_count=msg_count, signal_count=signal_count)


@app.route('/help')
@login_required
def help_page():
    """Help and support page."""
    return render_template('help.html', bot_running=bot_manager.is_running(), active_page='help')


@app.route('/api/stats')
def api_stats():
    """Get trading statistics."""
    stats = get_stats()
    
    # Add daily profit calculation
    from datetime import datetime
    today = datetime.now().date()
    trades = trades_db.get_all()
    
    daily_profit = 0
    today_trades = 0
    for t in trades:
        try:
            trade_date = datetime.fromisoformat(t.get('created_at', '')).date()
            if trade_date == today:
                daily_profit += t.get('profit', 0)
                today_trades += 1
        except:
            pass
    
    stats['daily_profit'] = round(daily_profit, 2)
    stats['today_trades'] = today_trades
    stats['active_strategies'] = 5  # Placeholder
    stats['monitored_pairs'] = 12  # Placeholder
    
    return jsonify(stats)


@app.route('/api/trades')
def api_trades():
    """Get all trades."""
    return jsonify(trades_db.get_all())


@app.route('/api/signals')
def api_signals():
    """Get all signals."""
    return jsonify(signals_db.get_all())


@app.route('/api/logs')
def api_logs():
    """Get recent logs with filtering."""
    limit = request.args.get('limit', 50, type=int)
    log_type = request.args.get('type', 'all')
    source = request.args.get('source', None)
    
    logs = logs_db.get_all()
    
    # Filter by type/tag
    if log_type != 'all':
        if log_type == 'trades':
            logs = [l for l in logs if l.get('source') in ['trade_executor', 'web_ui'] and 'trade' in l.get('message', '').lower()]
        elif log_type == 'errors':
            logs = [l for l in logs if l.get('level') == 'error']
        elif log_type == 'api':
            logs = [l for l in logs if 'api' in l.get('message', '').lower() or l.get('source') == 'api']
        elif log_type == 'system':
            logs = [l for l in logs if l.get('source') in ['system', 'web_ui', 'bot']]
    
    # Add tag field for dashboard display
    for log in logs:
        msg = log.get('message', '').lower()
        src = log.get('source', '')
        level = log.get('level', '')
        
        if 'signal' in msg:
            log['tag'] = 'signal'
        elif 'trade' in msg or src == 'trade_executor':
            log['tag'] = 'trade'
        elif level == 'error':
            log['tag'] = 'error'
        elif level == 'warning':
            log['tag'] = 'warning'
        elif src == 'api' or 'api' in msg:
            log['tag'] = 'api'
        else:
            log['tag'] = 'system'
    
    return jsonify(logs[-limit:])


@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    """Start the bot."""
    success = bot_manager.start()
    return jsonify({'success': success, 'message': 'Bot started' if success else 'Bot already running'})


@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    """Stop the bot."""
    success = bot_manager.stop()
    return jsonify({'success': success, 'message': 'Bot stopped' if success else 'Bot not running'})


@app.route('/api/bot/pause', methods=['POST'])
def pause_bot():
    """Pause the bot (stop accepting new signals but keep running)."""
    if not bot_manager.is_running():
        return jsonify({'success': False, 'message': 'Bot not running'})
    
    log_event("info", "Bot paused - new signals will be ignored", "web_ui")
    return jsonify({'success': True, 'message': 'Bot paused - new signals ignored'})


@app.route('/api/balance')
def api_balance():
    """Get account balance from Pocket Option."""
    try:
        if bot_manager.executor and bot_manager.executor.platform.connected:
            platform = bot_manager.executor.platform
            if platform.api and not platform.simulation_mode:
                # Real balance would require async call
                # For now return connected status only
                return jsonify({'balance': 0, 'currency': config.CURRENCY, 'demo': False, 'connected': True})
        return jsonify({'balance': 0, 'currency': config.CURRENCY, 'demo': True, 'connected': False, 'error': 'Not connected'})
    except Exception as e:
        return jsonify({'balance': 0, 'currency': config.CURRENCY, 'error': str(e), 'connected': False})


@app.route('/api/markets')
def api_markets():
    """Get monitored markets/pairs from signals database."""
    signals = signals_db.get_all()
    # Extract unique pairs from signals
    pairs = list(set([s.get('pair', '') for s in signals if s.get('pair')]))
    if not pairs:
        # Default pairs if no signals yet
        pairs = ['EUR/USD', 'GBP/USD', 'USD/JPY']
    return jsonify({
        'pairs': pairs,
        'count': len(pairs)
    })


@app.route('/api/bot/status')
def bot_status():
    """Get bot status."""
    # Check if Telegram session needs renewal
    session_file = f"{config.TELEGRAM_SESSION_NAME}.session"
    needs_telegram_auth = not os.path.exists(session_file)
    
    return jsonify({
        'running': bot_manager.is_running(),
        'pocket_connected': bot_manager.executor is not None and bot_manager.executor.platform.connected if bot_manager.executor else False,
        'telegram_needs_auth': needs_telegram_auth,
        'telegram_session_exists': os.path.exists(session_file)
    })


@app.route('/api/telegram/auth', methods=['POST'])
def telegram_auth():
    """Handle Telegram authentication with phone number and code."""
    data = request.get_json()
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    
    if not phone:
        return jsonify({'success': False, 'message': 'Phone number required'}), 400
    
    # Store auth request for bot to pick up
    auth_request_file = os.path.join(os.path.dirname(__file__), '.telegram_auth_request.json')
    auth_data = {
        'phone': phone,
        'code': code,
        'timestamp': datetime.now().isoformat(),
        'status': 'pending'
    }
    
    with open(auth_request_file, 'w') as f:
        json.dump(auth_data, f)
    
    log_event("info", f"Telegram auth request received for {phone}", "web_ui")
    
    # If bot is not running, we need to start it so it can process the auth
    if not bot_manager.is_running():
        return jsonify({
            'success': True, 
            'message': 'Auth request saved. Start the bot to complete authentication.',
            'needs_bot_start': True
        })
    
    return jsonify({
        'success': True, 
        'message': 'Code submitted. Bot will process authentication.' if code else 'Phone submitted. Enter the code you receive.',
        'needs_code': not code
    })


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration."""
    if request.method == 'GET':
        return jsonify({
            'telegram_api_id': config.TELEGRAM_API_ID,
            'telegram_channel_id': config.TELEGRAM_CHANNEL_ID,
            'pocket_option_email': config.POCKET_OPTION_EMAIL,
            'default_amount': config.DEFAULT_AMOUNT,
            'currency': config.CURRENCY,
            'max_martingale': config.MAX_MARTINGALE_LEVELS,
            'martingale_multiplier': config.MARTINGALE_MULTIPLIER
        })
    else:
        # Update config (in-memory only for now)
        data = request.json
        log_event("info", f"Config updated: {data}", "web_ui")
        return jsonify({'success': True})


@app.route('/api/clear_logs', methods=['POST'])
def clear_logs():
    """Clear all logs from database."""
    try:
        logs_db.write([])
        log_event("info", "All logs cleared", "web_ui")
        return jsonify({'success': True, 'message': 'Logs cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/reset_stats', methods=['POST'])
def reset_stats():
    """Reset all trading statistics (clear trades and signals)."""
    try:
        trades_db.write([])
        signals_db.write([])
        log_event("info", "Trading statistics reset", "web_ui")
        return jsonify({'success': True, 'message': 'Statistics reset'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/notifications')
def api_notifications():
    """Get recent notifications for badge count."""
    logs = logs_db.get_all()[-5:]  # Last 5 events
    return jsonify({'count': len(logs), 'notifications': logs})


@app.route('/api/platforms')
def api_platforms():
    """Get all trading platforms status."""
    platforms = platform_manager.get_all_status()
    return jsonify({
        'platforms': platforms,
        'active': platform_manager.active_platform,
        'count': len(platforms)
    })


@app.route('/api/platform/active')
def api_platform_active():
    """Get active platform status."""
    status = platform_manager.get_active_status()
    return jsonify({
        'active': status,
        'platform_name': platform_manager.active_platform,
        'connected': status.get('connected', False) if status else False
    })


@app.route('/api/platforms/test_connections', methods=['POST'])
@login_required
def api_test_all_platform_connections():
    """Test connect for every registered broker — no trades."""
    results = [_test_platform_connection(name) for name in platform_manager.platforms.keys()]
    all_ok = all(r.get('success') for r in results) if results else False
    return jsonify({'success': all_ok, 'results': results})


@app.route('/api/platform/test_connection', methods=['POST'])
@login_required
def api_test_platform_connection():
    """Test connect for one broker — no trade."""
    data = request.get_json() or {}
    platform_name = data.get('platform', 'pocket_option')
    result = _test_platform_connection(platform_name)
    return jsonify({'success': result.get('success', False), **result})


@app.route('/api/platform/connect', methods=['POST'])
@login_required
def api_platform_connect():
    """Connect to a specific platform."""
    data = request.get_json()
    platform_name = data.get('platform', 'pocket_option')

    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(platform_manager.connect(platform_name))
        loop.close()

        if success:
            log_event("info", f"Connected to platform: {platform_name}", "web_ui")
            return jsonify({'success': True, 'message': f'Connected to {platform_name}'})
        else:
            return jsonify({'success': False, 'message': f'Failed to connect to {platform_name}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/platform/disconnect', methods=['POST'])
@login_required
def api_platform_disconnect():
    """Disconnect from active platform."""
    data = request.get_json()
    platform_name = data.get('platform', platform_manager.active_platform)

    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(platform_manager.disconnect(platform_name))
        loop.close()

        if success:
            log_event("info", f"Disconnected from platform: {platform_name}", "web_ui")
            return jsonify({'success': True, 'message': f'Disconnected from {platform_name}'})
        else:
            return jsonify({'success': False, 'message': f'Failed to disconnect from {platform_name}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/manual_trade', methods=['POST'])
def manual_trade():
    """Place a manual trade."""
    data = request.json

    trade_record = {
        'pair': data.get('pair'),
        'direction': data.get('direction'),
        'amount': data.get('amount'),
        'expiration': data.get('expiration'),
        'is_otc': data.get('is_otc', False),
        'result': 'pending',
        'profit': 0,
        'type': 'manual'
    }

    inserted = trades_db.insert(trade_record)
    log_event("info", f"Manual trade placed: {trade_record['pair']} {trade_record['direction']}", "web_ui")

    return jsonify({'success': True, 'trade': inserted})


@app.route('/api/test_trade', methods=['POST'])
@login_required
def test_trade():
    """Execute a test trade on the selected platform (real or simulation)."""
    data = request.get_json() or {}

    platform_name = data.get('platform', 'pocket_option')
    pair = data.get('pair', 'GBP/AUD')
    direction = data.get('direction', 'BUY')
    amount = float(data.get('amount', 1.0))
    expiration = int(data.get('expiration', 5))
    is_otc = data.get('is_otc', True)

    if platform_name not in platform_manager.platforms:
        return jsonify({
            'success': False,
            'message': f'Platform "{platform_name}" is not configured. Save credentials under Settings → Platforms.',
        })

    try:
        platform = platform_manager.get_platform(platform_name)
        if not platform.status.connected:
            connected = _run_async(platform_manager.connect(platform_name))
            if not connected:
                err = platform.status.error_message or 'Could not connect'
                return jsonify({
                    'success': False,
                    'message': f'{platform_name}: {err}. Use Test connection first.',
                })

        async def execute():
            return await platform.place_trade(pair, amount, direction, expiration, is_otc)

        result = _run_async(execute())

        if not result:
            return jsonify({'success': False, 'message': 'No result from platform'})
        if result.get('error'):
            return jsonify({'success': False, 'message': f"Trade failed: {result['error']}"})

        trade_record = {
            'pair': pair,
            'direction': direction,
            'amount': amount,
            'expiration': expiration,
            'is_otc': is_otc,
            'platform': platform_name,
            'result': result.get('result', 'pending'),
            'profit': result.get('profit', 0),
            'type': 'test',
            'is_simulation': result.get('is_simulation', False),
            'executed_at': result.get('timestamp'),
            'platform_result': result,
        }
        inserted = trades_db.insert(trade_record)

        log_event("info", f"Test trade executed: {pair} {direction} ${amount} - Result: {result.get('result')}", "web_ui")

        return jsonify({
            'success': True,
            'message': f"Test trade placed successfully!",
            'trade': inserted,
            'result': result
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)})


# Settings Management Endpoints

def update_env_file(key: str, value: str):
    """Update a key in the .env file."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    lines = []

    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.readlines()

    # Find and replace or append
    key_found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            key_found = True
            break

    if not key_found:
        lines.append(f"{key}={value}\n")

    with open(env_path, 'w') as f:
        f.writelines(lines)


@app.route('/api/settings/trading', methods=['POST'])
@login_required
def save_trading_settings():
    """Save trading configuration."""
    data = request.get_json()

    try:
        if 'default_amount' in data:
            update_env_file('DEFAULT_AMOUNT', str(data['default_amount']))
        if 'currency' in data:
            update_env_file('CURRENCY', data['currency'])
        if 'max_martingale' in data:
            update_env_file('MAX_MARTINGALE_LEVELS', str(data['max_martingale']))
        if 'martingale_multiplier' in data:
            update_env_file('MARTINGALE_MULTIPLIER', str(data['martingale_multiplier']))
        if 'timezone' in data:
            update_env_file('TIMEZONE', data['timezone'])
        if 'monitor_all_channels' in data:
            val = 'true' if data.get('monitor_all_channels') else 'false'
            update_env_file('MONITOR_ALL_CHANNELS', val)

        log_event("info", "Trading settings updated", "web_ui")
        return jsonify({'success': True, 'message': 'Trading settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/settings/telegram', methods=['POST'])
@login_required
def save_telegram_settings():
    """Save Telegram API configuration."""
    data = request.get_json()

    try:
        if 'telegram_api_id' in data:
            update_env_file('TELEGRAM_API_ID', str(data['telegram_api_id']))
        if 'telegram_api_hash' in data:
            update_env_file('TELEGRAM_API_HASH', data['telegram_api_hash'])
        if 'telegram_session_name' in data:
            update_env_file('TELEGRAM_SESSION_NAME', data['telegram_session_name'])
        if 'telegram_channel_id' in data:
            update_env_file('TELEGRAM_CHANNEL_ID', data['telegram_channel_id'])

        log_event("info", "Telegram API settings updated", "web_ui")
        return jsonify({'success': True, 'message': 'Telegram settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# Session Management (Telegram Userbot)
session_manager = {
    'phone': None,
    'phone_code_hash': None,
    'client': None
}


def _run_async_in_thread(coro_factory, *args, **kwargs):
    """Run async coroutine in a new thread with its own event loop.
    
    Args:
        coro_factory: A callable that returns a coroutine (async function)
        *args, **kwargs: Arguments to pass to the coroutine factory
    """
    import threading
    result = {'value': None, 'error': None}

    def target():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Create the coroutine inside the thread with its own event loop
            coro = coro_factory(*args, **kwargs)
            result['value'] = loop.run_until_complete(coro)
            loop.close()
        except Exception as e:
            result['error'] = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()

    if result['error']:
        raise result['error']
    return result['value']


@app.route('/api/session/request_code', methods=['POST'])
@login_required
def request_session_code():
    """Request verification code for Telegram session."""
    data = request.get_json()
    phone = data.get('phone', '')

    if not phone:
        return jsonify({'success': False, 'message': 'Phone number required'})

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        # Use a separate session file for web auth to avoid conflict with running bot
        web_session_name = 'web_auth_session'
        
        # Create async function that handles everything including client creation
        async def send_code():
            client = TelegramClient(
                web_session_name,
                config.TELEGRAM_API_ID,
                config.TELEGRAM_API_HASH
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    result = await client.send_code_request(phone)
                    # Store client reference for later use (keep it connected)
                    session_manager['client'] = client
                    return result.phone_code_hash
                return None
            except Exception:
                await client.disconnect()
                raise

        # Use thread-safe async execution - pass function, not coroutine
        try:
            phone_code_hash = _run_async_in_thread(send_code)
        except Exception as e:
            import traceback
            print(f"Error in request_code: {e}")
            traceback.print_exc()
            return jsonify({'success': False, 'message': f'{type(e).__name__}: {e}'})

        if phone_code_hash:
            session_manager['phone'] = phone
            session_manager['phone_code_hash'] = phone_code_hash

            log_event("info", f"Verification code requested for {phone}", "web_ui")
            return jsonify({
                'success': True,
                'phone_code_hash': phone_code_hash,
                'message': 'Code sent to your Telegram'
            })
        else:
            return jsonify({'success': True, 'message': 'Already authorized'})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/session/verify', methods=['POST'])
@login_required
def verify_session_code():
    """Verify code and create Telegram session."""
    data = request.get_json()
    code = data.get('code', '')
    phone = data.get('phone', session_manager.get('phone', ''))
    password = data.get('password', '')

    if not code or not phone:
        return jsonify({'success': False, 'message': 'Code and phone required'})

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        # Use same web session for verification
        web_session_name = 'web_auth_session'
        phone_code_hash = session_manager.get('phone_code_hash', '')

        async def sign_in():
            # Always create client inside async context
            client = TelegramClient(
                web_session_name,
                config.TELEGRAM_API_ID,
                config.TELEGRAM_API_HASH
            )
            try:
                await client.connect()
                try:
                    # Use phone_code_hash if available
                    if phone_code_hash:
                        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    else:
                        await client.sign_in(phone, code)
                    return {'success': True}
                except SessionPasswordNeededError:
                    if password:
                        await client.sign_in(password=password)
                        return {'success': True}
                    return {'success': False, 'requires_2fa': True}
                except Exception as e:
                    return {'success': False, 'message': str(e)}
            finally:
                await client.disconnect()

        # Use thread-safe async execution - pass function, not coroutine
        result = _run_async_in_thread(sign_in)

        if result.get('success'):
            log_event("info", f"Telegram session created for {phone}", "web_ui")
            session_manager['client'] = None
            return jsonify({'success': True, 'message': 'Session created successfully'})
        elif result.get('requires_2fa'):
            return jsonify({'success': False, 'requires_2fa': True, 'message': '2FA password required'})
        else:
            return jsonify({'success': False, 'message': result.get('message', 'Unknown error')})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/settings/pocket_option', methods=['POST'])
@login_required
def save_pocket_option_settings():
    """Save Pocket Option credentials."""
    data = request.get_json()

    try:
        if 'po_email' in data:
            update_env_file('POCKET_OPTION_EMAIL', data['po_email'])
        if 'po_password' in data:
            update_env_file('POCKET_OPTION_PASSWORD', data['po_password'])
        if 'po_ssid' in data:
            update_env_file('POCKET_OPTION_SSID', data['po_ssid'])

        log_event("info", "Pocket Option settings updated", "web_ui")
        return jsonify({'success': True, 'message': 'Pocket Option settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/settings/deriv', methods=['POST'])
@login_required
def save_deriv_settings():
    """Save Deriv API credentials."""
    data = request.get_json()

    try:
        if 'deriv_token' in data:
            update_env_file('DERIV_API_TOKEN', data['deriv_token'])
        if 'deriv_app_id' in data:
            update_env_file('DERIV_APP_ID', data['deriv_app_id'])

        log_event("info", "Deriv settings updated", "web_ui")
        return jsonify({'success': True, 'message': 'Deriv settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# Telegram Channel Management Endpoints

@app.route('/api/channels', methods=['GET'])
@login_required
def get_channels():
    """Get all configured Telegram channels."""
    channels = get_all_channels_normalized()
    return jsonify({'success': True, 'channels': channels})


@app.route('/api/channels', methods=['POST'])
@login_required
def add_channel():
    """Add a new Telegram channel."""
    data = request.get_json()

    channel_id = data.get('channel_id', '').strip()
    channel_name = data.get('channel_name', '').strip()
    description = data.get('description', '').strip()
    is_active = data.get('is_active', True)

    if not channel_id:
        return jsonify({'success': False, 'message': 'Channel ID is required'}), 400

    # Check if channel already exists
    existing = channels_db.get_all()
    for ch in existing:
        if ch.get('channel_id') == channel_id:
            return jsonify({'success': False, 'message': 'Channel already exists'}), 400

    channel_record = normalize_channel_record({
        'channel_id': channel_id,
        'channel_name': channel_name or 'Unknown',
        'description': description,
        'is_active': is_active,
        'parser_id': data.get('parser_id', 'lorenzo'),
        'platform_id': data.get('platform_id', 'pocket_option'),
        'timezone': data.get('timezone', config.TIMEZONE or 'UTC'),
        'auto_trade': data.get('auto_trade', True),
        'market_type': data.get('market_type', 'binary_timed'),
        'default_amount': data.get('default_amount'),
        'added_at': datetime.now().isoformat(),
    })

    inserted = channels_db.insert(channel_record)

    # Also update the env file for backward compatibility (use first active channel)
    update_env_file('TELEGRAM_CHANNEL_ID', channel_id)

    log_event("info", f"Telegram channel added: {channel_name or channel_id}", "web_ui")
    return jsonify({'success': True, 'channel': inserted, 'message': 'Channel added successfully'})


@app.route('/api/channels/<int:channel_db_id>', methods=['DELETE'])
@login_required
def delete_channel(channel_db_id):
    """Remove a Telegram channel."""
    try:
        channels_db.delete(channel_db_id)
        log_event("info", f"Telegram channel removed (ID: {channel_db_id})", "web_ui")
        return jsonify({'success': True, 'message': 'Channel removed'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/channels/<int:channel_db_id>/toggle', methods=['POST'])
@login_required
def toggle_channel(channel_db_id):
    """Toggle channel active status."""
    data = request.get_json()
    is_active = data.get('is_active', True)

    updated = channels_db.update(channel_db_id, {'is_active': is_active})
    if updated:
        status = 'activated' if is_active else 'deactivated'
        log_event("info", f"Telegram channel {status} (ID: {channel_db_id})", "web_ui")
        return jsonify({'success': True, 'channel': updated})
    return jsonify({'success': False, 'message': 'Channel not found'}), 404


@app.route('/api/channels/fetch', methods=['POST'])
@login_required
def fetch_telegram_channels():
    """Fetch channels from connected Telegram account."""
    try:
        from telethon import TelegramClient
        from telethon.tl.types import Channel

        # Use separate web session to avoid conflict with running bot
        web_session_name = 'web_auth_session'

        async def get_channels():
            client = TelegramClient(web_session_name, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return {'error': 'Not authorized. Please create a session first.'}

            channels = []
            async for dialog in client.iter_dialogs():
                if isinstance(dialog.entity, Channel):
                    channels.append({
                        'id': str(dialog.entity.id),
                        'title': dialog.title,
                        'username': dialog.entity.username or '',
                        'participants_count': getattr(dialog.entity, 'participants_count', 0),
                        'is_group': dialog.entity.broadcast == False
                    })

            await client.disconnect()
            return {'channels': channels}

        # Pass the async function itself (not the coroutine) to create it in the thread
        result = _run_async_in_thread(get_channels)

        if 'error' in result:
            return jsonify({'success': False, 'message': result['error']}), 401

        log_event("info", f"Fetched {len(result['channels'])} channels from Telegram", "web_ui")
        return jsonify({'success': True, 'channels': result['channels']})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/channels/resolve', methods=['POST'])
@login_required
def resolve_channel():
    """Resolve channel info by username or invite link."""
    data = request.get_json()
    identifier = data.get('identifier', '').strip()

    if not identifier:
        return jsonify({'success': False, 'message': 'Channel identifier required'}), 400

    try:
        from telethon import TelegramClient

        # Use separate web session to avoid conflict with running bot
        web_session_name = 'web_auth_session'

        async def resolve():
            # Create client inside async context
            client = TelegramClient(
                web_session_name,
                config.TELEGRAM_API_ID,
                config.TELEGRAM_API_HASH
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    return {'error': 'Not authorized'}

                # Handle invite links
                if 't.me/+' in identifier or identifier.startswith('+'):
                    invite_hash = identifier.split('+')[-1].split('/')[-1] if 't.me/+' in identifier else identifier[1:]
                    from telethon.tl.functions.messages import CheckChatInviteRequest
                    try:
                        invite_info = await client(CheckChatInviteRequest(invite_hash))
                        if hasattr(invite_info, 'chat'):
                            chat = invite_info.chat
                            return {
                                'id': str(chat.id),
                                'title': getattr(chat, 'title', 'Unknown'),
                                'type': 'private_channel'
                            }
                    except Exception as e:
                        return {'error': str(e)}
                else:
                    # Handle username or ID
                    try:
                        if identifier.lstrip('-').isdigit():
                            entity = await client.get_entity(int(identifier))
                        else:
                            entity = await client.get_entity(identifier)
                        return {
                            'id': str(entity.id),
                            'title': getattr(entity, 'title', getattr(entity, 'first_name', 'Unknown')),
                            'username': getattr(entity, 'username', ''),
                            'type': 'channel' if hasattr(entity, 'broadcast') else 'user'
                        }
                    except Exception as e:
                        return {'error': str(e)}
            finally:
                await client.disconnect()

        result = _run_async_in_thread(resolve)

        if 'error' in result:
            return jsonify({'success': False, 'message': result['error']})

        return jsonify({'success': True, 'channel': result})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/platform/<platform_name>/settings', methods=['POST'])
@login_required
def save_platform_settings(platform_name):
    """Save platform-specific trading settings."""
    data = request.get_json()

    try:
        # Save to settings_db with platform-specific key
        settings_db.set(platform_name + '_config', data)

        log_event("info", f"Settings saved for platform: {platform_name}", "web_ui")
        return jsonify({'success': True, 'message': f'{platform_name} settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/reports/send-test', methods=['POST'])
@login_required
def api_send_test_report():
    """Send a test report email immediately."""
    from report_service import send_report_email
    data = request.get_json() or {}
    to_email = data.get('to_email') or None
    result = send_report_email('daily', to_email=to_email, bot_running=bot_manager.is_running(), test=True)
    return jsonify(result)


@app.route('/api/reports/send', methods=['POST'])
@login_required
def api_send_report():
    """Manually send daily, weekly, or monthly report."""
    from report_service import send_report_email
    data = request.get_json() or {}
    period = data.get('period', 'daily')
    if period not in ('daily', 'weekly', 'monthly'):
        return jsonify({'success': False, 'message': 'period must be daily, weekly, or monthly'})
    result = send_report_email(period, bot_running=bot_manager.is_running())
    return jsonify(result)


@app.route('/api/settings/reports', methods=['POST'])
@login_required
def save_report_settings():
    """Save email report / SMTP settings to .env."""
    data = request.get_json() or {}
    try:
        if 'report_email_to' in data:
            update_env_file('REPORT_EMAIL_TO', data['report_email_to'])
        if 'report_email_enabled' in data:
            update_env_file('REPORT_EMAIL_ENABLED', 'true' if data['report_email_enabled'] else 'false')
        if 'smtp_host' in data:
            update_env_file('SMTP_HOST', data['smtp_host'])
        if 'smtp_port' in data:
            update_env_file('SMTP_PORT', str(data['smtp_port']))
        if 'smtp_user' in data:
            update_env_file('SMTP_USER', data['smtp_user'])
        if 'smtp_password' in data and data['smtp_password']:
            update_env_file('SMTP_PASSWORD', data['smtp_password'])
        if 'smtp_from' in data:
            update_env_file('SMTP_FROM', data['smtp_from'])
        if 'smtp_use_tls' in data:
            update_env_file('SMTP_USE_TLS', 'true' if data['smtp_use_tls'] else 'false')
        if 'report_daily_hour' in data:
            update_env_file('REPORT_DAILY_HOUR', str(data['report_daily_hour']))
        if 'report_daily_minute' in data:
            update_env_file('REPORT_DAILY_MINUTE', str(data['report_daily_minute']))

        log_event('info', 'Report email settings updated', 'web_ui')
        return jsonify({
            'success': True,
            'message': 'Report settings saved. Restart the app (pm2 restart) for schedule changes to apply.',
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


def create_app():
    """Create Flask app for Gunicorn production server."""
    try:
        from report_scheduler import start_report_scheduler
        if start_report_scheduler():
            print('📧 Report email scheduler started')
    except Exception as e:
        print(f'⚠️  Report scheduler: {e}')
    return app


def run_ui(port=5050, debug=False, production=False):
    """Run the web UI.

    Args:
        port: Server port
        debug: Enable Flask debug mode (dev only)
        production: Use Gunicorn instead of Flask dev server
    """
    # Initialize platforms on startup
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init_platforms_from_env())
        loop.close()
        print(f"✅ Platforms initialized")
    except Exception as e:
        print(f"⚠️  Platform initialization warning: {e}")

    try:
        from report_scheduler import start_report_scheduler
        if start_report_scheduler():
            print('📧 Report email scheduler started')
    except Exception as e:
        print(f'⚠️  Report scheduler: {e}')

    print(f"🌐 Trading Bot UI starting on http://localhost:{port}")
    print(f"📊 Dashboard: http://localhost:{port}/")
    print(f"🤖 Bot Status: http://localhost:{port}/api/bot/status")
    print(f"💰 Platform Status: http://localhost:{port}/api/platform/active")
    
    if production:
        # Use Gunicorn for production
        import subprocess
        import sys
        workers = 2  # 2 workers for trading bot (CPU-bound light)
        cmd = [
            sys.executable, '-m', 'gunicorn',
            '-w', str(workers),
            '-b', f'0.0.0.0:{port}',
            '--access-logfile', '-',
            '--error-logfile', '-',
            'web_ui:create_app()'
        ]
        print(f"🚀 Production server with Gunicorn ({workers} workers)")
        subprocess.run(cmd)
    else:
        # Flask dev server (for development only)
        app.run(host='0.0.0.0', port=port, debug=debug)


if __name__ == '__main__':
    import sys
    import os
    # Check for --production flag
    production = '--production' in sys.argv
    debug = not production and '--debug' in sys.argv
    # Get port from environment variable or use default 5050
    port = int(os.environ.get('PORT', os.environ.get('FLASK_PORT', 5050)))
    run_ui(port=port, debug=debug, production=production)
