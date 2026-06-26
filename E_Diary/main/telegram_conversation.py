"""
In-memory conversation state manager for Telegram bot multi-turn flows.

States:
  IDLE                          — No active conversation
  AWAITING_REMINDER_YESNO       — Asked if they want a reminder, waiting for yes/no
  AWAITING_REMINDER_DETAILS     — Asked for reminder details, waiting for description/frequency/etc
"""

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# chat_id -> state dict
_conversations: dict = {}


def get_state(chat_id: str) -> dict:
    return _conversations.get(chat_id, {})


def set_state(chat_id: str, state: str, **kwargs):
    _conversations[chat_id] = {'state': state, **kwargs}


def clear_state(chat_id: str):
    _conversations.pop(chat_id, None)


def is_idle(chat_id: str) -> bool:
    return _conversations.get(chat_id, {}).get('state') in (None, 'IDLE')


def set_idle(chat_id: str):
    set_state(chat_id, 'IDLE')


def parse_yes_no(text: str) -> Optional[bool]:
    text = text.strip().lower()
    if text in ('yes', 'y', 'yeah', 'sure', 'ok', 'okay', 'please', 'yea', 'yep'):
        return True
    if text in ('no', 'n', 'nah', 'nope', 'not', 'dont', 'don\'t', 'no need', 'skip'):
        return False
    return None
