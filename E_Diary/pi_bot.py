import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

API_BASE_URL = os.getenv('API_BASE_URL')
API_TOKEN = os.getenv('API_TOKEN')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '2'))
POLL_TIMEOUT = int(os.getenv('POLL_TIMEOUT', '15'))
LIMIT = int(os.getenv('POLL_LIMIT', '50'))

TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'


def _headers():
    return {
        'Authorization': f'Bearer {API_TOKEN}',
        'Connection': 'close',
    }


def send_telegram(chat_id, text):
    try:
        t0 = time.time()
        r = requests.post(
            f'{TELEGRAM_API}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
            timeout=POLL_TIMEOUT,
        )
        ok = r.json().get('ok', False)
        elapsed = time.time() - t0
        if elapsed > 2:
            logger.warning(f'SendTelegram took {elapsed:.1f}s for chat {chat_id}')
        return ok
    except Exception as e:
        logger.error(f'SendTelegram failed for chat {chat_id}: {e}')
        return False


def mark_sent(msg_ids):
    if not msg_ids:
        return []
    ids = [msg_ids] if isinstance(msg_ids, int) else list(msg_ids)
    try:
        r = requests.post(
            f'{API_BASE_URL}/api/mark-sent/',
            headers=_headers(),
            json={'ids': ids},
            timeout=POLL_TIMEOUT,
        )
        data = r.json()
        return data.get('sent', [])
    except Exception as e:
        logger.error(f'Mark-sent batch failed for ids {ids}: {e}')
        return []


def poll():
    logger.info(
        f'Polling {API_BASE_URL}/api/pending-messages/ '
        f'every {POLL_INTERVAL}s (timeout={POLL_TIMEOUT}s, limit={LIMIT})'
    )
    while True:
        loop_start = time.time()
        try:
            r = requests.get(
                f'{API_BASE_URL}/api/pending-messages/?limit={LIMIT}',
                headers=_headers(),
                timeout=POLL_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning(f'API error: {r.status_code} {r.text[:200]}')
                time.sleep(POLL_INTERVAL)
                continue

            messages = r.json().get('messages', [])
            if messages:
                logger.info(f'Got {len(messages)} pending message(s)')
                sent_ids = []
                for msg in messages:
                    ok = send_telegram(msg['chat_id'], msg['text'])
                    if ok:
                        sent_ids.append(msg['id'])
                    else:
                        logger.warning(f'Failed to send msg {msg["id"]} to {msg["chat_id"]}')
                if sent_ids:
                    confirmed = mark_sent(sent_ids)
                    for mid in confirmed:
                        logger.info(f'Sent msg {mid}')
            else:
                elapsed = time.time() - loop_start
                if elapsed > 3:
                    logger.debug(f'Poll OK (empty, took {elapsed:.1f}s)')
        except requests.Timeout:
            logger.warning(f'Poll timed out (>{POLL_TIMEOUT}s)')
        except requests.ConnectionError as e:
            logger.error(f'Poll connection error: {e}')
        except Exception as e:
            logger.exception(f'Poll error: {e}')

        elapsed = time.time() - loop_start
        sleep = POLL_INTERVAL - elapsed
        if sleep > 0:
            time.sleep(sleep)


if __name__ == '__main__':
    if not all([API_BASE_URL, API_TOKEN, BOT_TOKEN]):
        logger.error('Missing required env vars: API_BASE_URL, API_TOKEN, TELEGRAM_BOT_TOKEN')
        exit(1)
    poll()
