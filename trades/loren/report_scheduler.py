"""Scheduled daily / weekly / monthly email reports."""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
from report_service import send_report_email

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock_file = None

LOCK_PATH = Path(__file__).parent / 'data' / '.report_scheduler.lock'


def _bot_running() -> bool:
    try:
        import importlib
        mod = importlib.import_module('web_ui')
        return mod.bot_manager.is_running()
    except Exception:
        return False


def _job_send(period: str) -> None:
    cfg = Config()
    if not cfg.REPORT_EMAIL_ENABLED:
        logger.info('Report job skipped: REPORT_EMAIL_ENABLED=false')
        return
    result = send_report_email(period, bot_running=_bot_running())
    if result.get('success'):
        logger.info('Scheduled %s report sent to %s', period, result.get('recipient'))
    else:
        logger.warning('Scheduled %s report failed: %s', period, result.get('message'))


def start_report_scheduler() -> bool:
    """Start APScheduler in this process only (file lock for multi-worker)."""
    global _scheduler, _lock_file

    if _scheduler is not None:
        return True

    cfg = Config()
    if not cfg.REPORT_EMAIL_ENABLED:
        logger.info('Report scheduler not started (REPORT_EMAIL_ENABLED=false)')
        return False

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _lock_file = open(LOCK_PATH, 'w')
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info('Report scheduler already running in another process')
        if _lock_file:
            _lock_file.close()
            _lock_file = None
        return False
    except OSError as e:
        logger.warning('Could not acquire report scheduler lock: %s', e)
        return False

    _scheduler = BackgroundScheduler(timezone='UTC')

    daily_h = cfg.REPORT_DAILY_HOUR
    daily_m = cfg.REPORT_DAILY_MINUTE

    _scheduler.add_job(
        lambda: _job_send('daily'),
        CronTrigger(hour=daily_h, minute=daily_m),
        id='report_daily',
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _job_send('weekly'),
        CronTrigger(day_of_week=cfg.REPORT_WEEKLY_DAY, hour=daily_h, minute=daily_m),
        id='report_weekly',
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _job_send('monthly'),
        CronTrigger(day=cfg.REPORT_MONTHLY_DAY, hour=daily_h, minute=daily_m),
        id='report_monthly',
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        'Report scheduler started (daily %02d:%02d UTC, weekly %s, monthly day %s)',
        daily_h, daily_m, cfg.REPORT_WEEKLY_DAY, cfg.REPORT_MONTHLY_DAY,
    )
    return True


def stop_report_scheduler() -> None:
    global _scheduler, _lock_file
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    if _lock_file:
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
            _lock_file.close()
        except OSError:
            pass
        _lock_file = None


def main():
    parser = argparse.ArgumentParser(description='TelVictory report email tools')
    parser.add_argument('--test', action='store_true', help='Send a test report now')
    parser.add_argument('--period', choices=['daily', 'weekly', 'monthly'], help='Send one report now')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.test:
        r = send_report_email('daily', test=True)
        print(r)
        raise SystemExit(0 if r.get('success') else 1)

    if args.period:
        r = send_report_email(args.period)
        print(r)
        raise SystemExit(0 if r.get('success') else 1)

    print('Use --test or --period daily|weekly|monthly')


if __name__ == '__main__':
    main()
