"""
Telegram bot in polling mode — for local development only.
For production (PythonAnywhere), use webhook mode instead.
"""

import os
import time
import logging
import threading
import requests

from django.core.management.base import BaseCommand
from dotenv import load_dotenv

load_dotenv()

from main.telegram_handler import process_update
from main.management.commands.send_reminders import send_due_reminders

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
LAST_UPDATE_ID = 0
REMINDER_INTERVAL = 60


def reminder_loop():
    while True:
        try:
            sent = send_due_reminders(auto=True)
            if sent:
                logger.info(f'Auto-reminder: sent {sent} reminder(s)')
        except Exception as e:
            logger.exception(f'Reminder loop error: {e}')
        time.sleep(REMINDER_INTERVAL)


def run_polling():
    global LAST_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN:
        logger.error('TELEGRAM_BOT_TOKEN not set in .env')
        return

    logger.info('Telegram bot polling started...')
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates'

    reminder_thread = threading.Thread(target=reminder_loop, daemon=True)
    reminder_thread.start()
    logger.info(f'Reminder auto-sender started (interval: {REMINDER_INTERVAL}s, sends at 6:30 PM IST)')

    while True:
        try:
            params = {'timeout': 30, 'offset': LAST_UPDATE_ID + 1}
            r = requests.get(url, params=params, timeout=35)
            data = r.json()

            if not data.get('ok'):
                logger.error(f'Telegram API error: {data}')
                time.sleep(5)
                continue

            for update in data.get('result', []):
                if 'update_id' in update:
                    LAST_UPDATE_ID = max(LAST_UPDATE_ID, update['update_id'])
                process_update(update)

        except requests.Timeout:
            pass
        except requests.RequestException as e:
            logger.error(f'Polling error: {e}')
            time.sleep(5)
        except Exception as e:
            logger.exception(f'Unexpected error: {e}')
            time.sleep(5)


class Command(BaseCommand):
    help = 'Run the Telegram bot in polling mode (local dev only — use webhook for production)'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting Telegram bot (polling mode)...'))
        run_polling()
