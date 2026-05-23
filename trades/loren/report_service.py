"""Build and send TelVictory trading reports by email."""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from database import trades_db, signals_db, logs_db, channels_db, log_event

logger = logging.getLogger(__name__)

PERIOD_LABELS = {
    'daily': 'Daily',
    'weekly': 'Weekly',
    'monthly': 'Monthly',
    'test': 'Test',
}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace('Z', '+00:00')
        if '+' not in s[10:] and s.endswith(''):
            return datetime.fromisoformat(s)
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _record_dt(record: Dict) -> Optional[datetime]:
    for key in ('executed_at', 'created_at', 'updated_at'):
        dt = _parse_dt(record.get(key))
        if dt:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
    return None


def period_range(period: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime, str]:
    """Return (start, end, human label) in UTC."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if period == 'daily':
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        label = f"{start.strftime('%Y-%m-%d')} (UTC)"
    elif period == 'weekly':
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        label = f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')} (UTC)"
    elif period == 'monthly':
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if end.month == 1:
            start = end.replace(year=end.year - 1, month=12)
        else:
            start = end.replace(month=end.month - 1)
        label = f"{start.strftime('%B %Y')} (UTC)"
    else:  # test — last 24h snapshot
        end = now
        start = now - timedelta(hours=24)
        label = f"Last 24 hours (test at {now.strftime('%Y-%m-%d %H:%M UTC')})"

    return start, end, label


def _in_range(record: Dict, start: datetime, end: datetime) -> bool:
    dt = _record_dt(record)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt < end


def compute_period_stats(period: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    start, end, period_label = period_range(period, now)
    trades_all = trades_db.get_all()
    signals_all = signals_db.get_all()

    trades = [t for t in trades_all if _in_range(t, start, end)]
    signals = [s for s in signals_all if _in_range(s, start, end)]

    won = [t for t in trades if t.get('result') == 'win']
    lost = [t for t in trades if t.get('result') == 'loss']
    pending = [t for t in trades if t.get('result') not in ('win', 'loss')]
    profit = sum(float(t.get('profit') or 0) for t in trades)
    closed = len(won) + len(lost)

    by_platform: Dict[str, int] = {}
    for t in trades:
        p = t.get('platform') or 'unknown'
        by_platform[p] = by_platform.get(p, 0) + 1

    by_channel: Dict[str, int] = {}
    for s in signals:
        ch = s.get('source') or s.get('source_channel') or 'unknown'
        by_channel[ch] = by_channel.get(ch, 0) + 1

    errors = [
        lg for lg in logs_db.get_all()
        if _in_range(lg, start, end) and lg.get('level') == 'error'
    ]

    recent_trades = sorted(trades, key=lambda t: _record_dt(t) or datetime.min.replace(tzinfo=timezone.utc))[-10:]

    channels = channels_db.get_all()
    active_channels = len([c for c in channels if c.get('is_active', True)])

    return {
        'period': period,
        'period_label': period_label,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'trades_count': len(trades),
        'signals_count': len(signals),
        'won': len(won),
        'lost': len(lost),
        'pending': len(pending),
        'win_rate': round(len(won) / closed * 100, 1) if closed else 0,
        'profit': round(profit, 2),
        'by_platform': by_platform,
        'by_channel': by_channel,
        'errors_count': len(errors),
        'active_channels': active_channels,
        'recent_trades': recent_trades,
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }


def _fmt_money(v: float) -> str:
    sign = '+' if v >= 0 else ''
    return f"{sign}${v:.2f}"


def build_report_html(stats: Dict[str, Any], bot_running: bool = False) -> str:
    period_name = PERIOD_LABELS.get(stats['period'], stats['period'].title())
    profit = stats['profit']
    profit_color = '#22c55e' if profit >= 0 else '#ef4444'

    platform_rows = ''.join(
        f"<tr><td>{name.replace('_', ' ').title()}</td><td>{cnt}</td></tr>"
        for name, cnt in sorted(stats.get('by_platform', {}).items(), key=lambda x: -x[1])
    ) or '<tr><td colspan="2">No trades</td></tr>'

    channel_rows = ''.join(
        f"<tr><td>{ch}</td><td>{cnt}</td></tr>"
        for ch, cnt in sorted(stats.get('by_channel', {}).items(), key=lambda x: -x[1])[:15]
    ) or '<tr><td colspan="2">No signals</td></tr>'

    trade_rows = ''
    for t in reversed(stats.get('recent_trades', [])[-10:]):
        dt = _record_dt(t)
        when = dt.strftime('%m-%d %H:%M') if dt else '—'
        trade_rows += (
            f"<tr><td>{when}</td><td>{t.get('pair', '—')}</td>"
            f"<td>{t.get('direction', '—')}</td><td>{t.get('platform', '—')}</td>"
            f"<td>{t.get('result', '—')}</td><td>{_fmt_money(float(t.get('profit') or 0))}</td></tr>"
        )
    if not trade_rows:
        trade_rows = '<tr><td colspan="6">No trades in this period</td></tr>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Segoe UI,sans-serif;background:#0a0e14;color:#b4bcc8;padding:24px">
  <div style="max-width:640px;margin:0 auto;background:#1a212b;border:1px solid #2d3640;border-radius:12px;padding:28px">
    <h1 style="color:#f0f3f6;margin:0 0 4px">TelVictory {period_name} Report</h1>
    <p style="color:#7d8694;margin:0 0 24px">{stats['period_label']}</p>
    <p style="margin:0 0 20px">Bot: <strong style="color:{'#22c55e' if bot_running else '#ef4444'}">{'Running' if bot_running else 'Stopped'}</strong>
       · Generated {stats['generated_at']}</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      <tr style="background:#12171f">
        <td style="padding:12px;border:1px solid #2d3640"><strong>Trades</strong><br>{stats['trades_count']}</td>
        <td style="padding:12px;border:1px solid #2d3640"><strong>Signals</strong><br>{stats['signals_count']}</td>
        <td style="padding:12px;border:1px solid #2d3640"><strong>Win rate</strong><br>{stats['win_rate']}%</td>
        <td style="padding:12px;border:1px solid #2d3640"><strong>P/L</strong><br><span style="color:{profit_color}">{_fmt_money(profit)}</span></td>
      </tr>
      <tr>
        <td colspan="2" style="padding:12px;border:1px solid #2d3640">Wins: {stats['won']} · Losses: {stats['lost']} · Pending: {stats['pending']}</td>
        <td colspan="2" style="padding:12px;border:1px solid #2d3640">Errors: {stats['errors_count']} · Active channels: {stats['active_channels']}</td>
      </tr>
    </table>
    <h2 style="color:#f0f3f6;font-size:16px">By platform</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:14px">
      <tr style="background:#12171f"><th style="text-align:left;padding:8px;border:1px solid #2d3640">Platform</th><th style="text-align:left;padding:8px;border:1px solid #2d3640">Trades</th></tr>
      {platform_rows}
    </table>
    <h2 style="color:#f0f3f6;font-size:16px">Signals by channel</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:14px">
      <tr style="background:#12171f"><th style="text-align:left;padding:8px;border:1px solid #2d3640">Channel</th><th style="text-align:left;padding:8px;border:1px solid #2d3640">Signals</th></tr>
      {channel_rows}
    </table>
    <h2 style="color:#f0f3f6;font-size:16px">Recent trades</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr style="background:#12171f">
        <th style="padding:8px;border:1px solid #2d3640">Time</th><th style="padding:8px;border:1px solid #2d3640">Pair</th>
        <th style="padding:8px;border:1px solid #2d3640">Dir</th><th style="padding:8px;border:1px solid #2d3640">Platform</th>
        <th style="padding:8px;border:1px solid #2d3640">Result</th><th style="padding:8px;border:1px solid #2d3640">P/L</th>
      </tr>
      {trade_rows}
    </table>
    <p style="color:#7d8694;font-size:12px;margin-top:28px">TelVictory automated report · <a href="https://tel.bizinvestify.com" style="color:#3b82f6">Open dashboard</a></p>
  </div>
</body></html>"""


def build_report_text(stats: Dict[str, Any], bot_running: bool = False) -> str:
    period_name = PERIOD_LABELS.get(stats['period'], stats['period'])
    lines = [
        f"TelVictory {period_name} Report",
        f"Period: {stats['period_label']}",
        f"Bot: {'Running' if bot_running else 'Stopped'}",
        "",
        f"Trades: {stats['trades_count']} | Signals: {stats['signals_count']}",
        f"Wins: {stats['won']} | Losses: {stats['lost']} | Pending: {stats['pending']}",
        f"Win rate: {stats['win_rate']}% | P/L: {_fmt_money(stats['profit'])}",
        f"Errors: {stats['errors_count']} | Active channels: {stats['active_channels']}",
        "",
        "— TelVictory",
    ]
    return "\n".join(lines)


def send_report_email(
    period: str,
    *,
    to_email: Optional[str] = None,
    bot_running: bool = False,
    test: bool = False,
) -> Dict[str, Any]:
    """Build and send report. Returns {success, message, ...}."""
    cfg = Config()
    if not cfg.REPORT_EMAIL_ENABLED:
        return {'success': False, 'message': 'Email reports are disabled (REPORT_EMAIL_ENABLED=false)'}

    recipient = to_email or cfg.REPORT_EMAIL_TO
    if not recipient:
        return {'success': False, 'message': 'No recipient (set REPORT_EMAIL_TO)'}

    if not cfg.SMTP_HOST or not cfg.SMTP_USER:
        return {
            'success': False,
            'message': 'SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD in .env',
        }

    report_period = 'test' if test else period
    stats = compute_period_stats(report_period)
    period_name = PERIOD_LABELS.get(report_period, report_period.title())
    subject = f"TelVictory {period_name} Report — {stats['period_label']}"
    if test:
        subject = f"[TEST] {subject}"

    html_body = build_report_html(stats, bot_running=bot_running)
    text_body = build_report_text(stats, bot_running=bot_running)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = cfg.SMTP_FROM or cfg.SMTP_USER
    msg['To'] = recipient
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        if cfg.SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, context=context) as server:
                server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
                server.sendmail(msg['From'], [recipient], msg.as_string())
        else:
            with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT) as server:
                if cfg.SMTP_USE_TLS:
                    server.starttls(context=ssl.create_default_context())
                server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
                server.sendmail(msg['From'], [recipient], msg.as_string())

        log_event('info', f'Report email sent ({report_period}) to {recipient}', 'reports')
        return {
            'success': True,
            'message': f'Report sent to {recipient}',
            'period': report_period,
            'recipient': recipient,
            'stats': {
                'trades': stats['trades_count'],
                'signals': stats['signals_count'],
                'profit': stats['profit'],
            },
        }
    except Exception as e:
        logger.exception('Failed to send report email')
        log_event('error', f'Report email failed: {e}', 'reports')
        return {'success': False, 'message': str(e)}
