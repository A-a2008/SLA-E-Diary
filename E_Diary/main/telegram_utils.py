import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
BASE_URL = f'https://api.telegram.org/bot{BOT_TOKEN}'
GROUP_CHAT_ID = os.getenv('TELEGRAM_GROUP_CHAT_ID')


def send_message(chat_id, text, parse_mode='HTML'):
    if not BOT_TOKEN:
        logger.warning('TELEGRAM_BOT_TOKEN not set, skipping message')
        return False
    url = f'{BASE_URL}/sendMessage'
    try:
        r = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode,
        }, timeout=10)
        data = r.json()
        if data.get('ok'):
            return True
        logger.error(f'Telegram send_message failed: {data.get("description", "unknown error")}')
        return False
    except requests.RequestException as e:
        logger.error(f'Telegram send_message failed: {e}')
        return False


def set_webhook(url):
    if not BOT_TOKEN:
        logger.warning('TELEGRAM_BOT_TOKEN not set, skipping webhook')
        return
    try:
        r = requests.post(f'{BASE_URL}/setWebhook', json={'url': url}, timeout=10)
        logger.info(f'Webhook set: {r.json()}')
    except requests.RequestException as e:
        logger.error(f'Telegram set_webhook failed: {e}')


def delete_webhook():
    if not BOT_TOKEN:
        return
    try:
        requests.post(f'{BASE_URL}/deleteWebhook', timeout=10)
    except requests.RequestException as e:
        logger.error(f'Telegram delete_webhook failed: {e}')


def send_group_message(text, parse_mode='HTML'):
    if not BOT_TOKEN:
        logger.warning('TELEGRAM_BOT_TOKEN not set, skipping group message')
        return False
    if not GROUP_CHAT_ID:
        logger.warning('TELEGRAM_GROUP_CHAT_ID not set, skipping group message')
        return False
    return send_message(GROUP_CHAT_ID, text, parse_mode=parse_mode)
