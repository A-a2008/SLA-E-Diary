"""
Standalone script for Raspberry Pi.
Polls PythonAnywhere for pending outgoing messages and sends them via Telegram API.
Run: python pi_bot.py

Requirements: requests, python-dotenv
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

API_BASE_URL = os.getenv('API_BASE_URL')  # e.g. https://slaediary.pythonanywhere.com
API_TOKEN = os.getenv('API_TOKEN')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '2'))

TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'


def send_telegram(chat_id, text):
    try:
        r = requests.post(f'{TELEGRAM_API}/sendMessage', json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
        }, timeout=10)
        return r.json().get('ok', False)
    except Exception as e:
        logger.error(f'Send failed: {e}')
        return False


def mark_sent(msg_id):
    try:
        r = requests.post(
            f'{API_BASE_URL}/api/mark-sent/{msg_id}/',
            headers={'Authorization': f'Bearer {API_TOKEN}'},
            timeout=10,
        )
        return r.json().get('ok', False)
    except Exception as e:
        logger.error(f'Mark-sent failed: {e}')
        return False


def poll():
    logger.info(f'Polling {API_BASE_URL}/api/pending-messages/ every {POLL_INTERVAL}s')
    while True:
        try:
            r = requests.get(
                f'{API_BASE_URL}/api/pending-messages/',
                headers={'Authorization': f'Bearer {API_TOKEN}'},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(f'API error: {r.status_code}')
                time.sleep(POLL_INTERVAL)
                continue

            messages = r.json().get('messages', [])
            for msg in messages:
                ok = send_telegram(msg['chat_id'], msg['text'])
                if ok:
                    mark_sent(msg['id'])
                    logger.info(f'Sent msg {msg["id"]} to {msg["chat_id"]}')
                else:
                    logger.warning(f'Failed to send msg {msg["id"]}')
        except Exception as e:
            logger.error(f'Poll error: {e}')

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    if not all([API_BASE_URL, API_TOKEN, BOT_TOKEN]):
        logger.error('Missing required env vars: API_BASE_URL, API_TOKEN, TELEGRAM_BOT_TOKEN')
        exit(1)
    poll()
