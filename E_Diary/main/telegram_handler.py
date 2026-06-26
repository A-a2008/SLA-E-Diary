"""
Shared Telegram message processing logic.
Used by both the webhook view (production) and the polling bot (local dev).
"""

import datetime
import logging

from django.contrib.auth.models import User
from django.utils import timezone

from main.models import UserProfile, DiaryEntry, Reminder, ReminderFrequency
from main.telegram_utils import send_message, send_group_message
from main.telegram_bot_ai import (
    classify_message, extract_diary_entry, extract_reminder_details,
    match_case, create_entry_from_extraction, parse_date,
)
from main.telegram_conversation import (
    get_state, set_state, clear_state, parse_yes_no,
)

logger = logging.getLogger(__name__)


def handle_reminder_yesno(chat_id, text, state):
    answer = parse_yes_no(text)
    if answer is None:
        send_message(chat_id, 'Please reply with "yes" or "no".')
        return

    if not answer:
        send_message(chat_id, '✅ Noted. No reminder will be set.')
        clear_state(chat_id)
        return

    entry_id = state.get('diary_entry_id')
    if not entry_id:
        send_message(chat_id, 'Something went wrong. Please try again.')
        clear_state(chat_id)
        return

    try:
        entry = DiaryEntry.objects.get(id=entry_id)
    except DiaryEntry.DoesNotExist:
        send_message(chat_id, 'Something went wrong. Please try again.')
        clear_state(chat_id)
        return

    next_date_str = entry.next_date.strftime('%d-%m-%Y')
    set_state(chat_id, 'AWAITING_REMINDER_DETAILS',
              diary_entry_id=entry.id,
              next_date_str=next_date_str)
    send_message(chat_id,
                 f'Great! Please tell me the reminder details in one message:\n\n'
                 f'1. What task should the reminder be for?\n'
                 f'2. Frequency? (daily / alternate / twice_week / weekly)\n'
                 f'3. Enable ramp-up (daily last week)? (yes/no)\n'
                 f'4. Start date? (DD-MM-YYYY, default today)\n\n'
                 f'Example: "Prepare arguments, daily, yes ramp-up, start 01-07-2026"')


def handle_reminder_details(chat_id, text, state):
    next_date_str = state.get('next_date_str')
    entry_id = state.get('diary_entry_id')

    try:
        details = extract_reminder_details(text, next_date_str)
    except Exception as e:
        logger.exception(f'Reminder extraction failed: {e}')
        send_message(chat_id, 'Sorry, I couldn\'t understand those details. Please try again with the format: task, frequency (daily/alternate/twice_week/weekly), ramp-up (yes/no), start date (DD-MM-YYYY).')
        return

    start_on = parse_date(details.start_on) or datetime.date.today()

    if details.frequency not in ('daily', 'alternate', 'twice_week', 'weekly'):
        details.frequency = 'daily'

    try:
        entry = DiaryEntry.objects.get(id=entry_id)
    except DiaryEntry.DoesNotExist:
        send_message(chat_id, 'Something went wrong. Please try again.')
        clear_state(chat_id)
        return

    Reminder.objects.create(
        diary_entry=entry,
        task=details.task,
        start_on=start_on,
        frequency=details.frequency,
        ramp_up=details.ramp_up,
    )

    freq_label = dict(ReminderFrequency.choices).get(details.frequency, details.frequency)
    ramp_text = 'Enabled' if details.ramp_up else 'Disabled'
    send_message(chat_id,
                 f'✅ Reminder set!\n\n'
                 f'<b>Task:</b> {details.task}\n'
                 f'<b>Frequency:</b> {freq_label}\n'
                 f'<b>Ramp-up:</b> {ramp_text}\n'
                 f'<b>Start:</b> {start_on.strftime("%d-%m-%Y")}\n\n'
                 f'You will receive this reminder in the group at 6:30 PM on scheduled days.')
    clear_state(chat_id)


def handle_ai_diary_entry(chat_id, text, profile):
    try:
        extraction = extract_diary_entry(text)
    except Exception as e:
        logger.exception(f'Diary entry extraction failed: {e}')
        send_message(chat_id, 'Sorry, I had trouble processing that. Please try again.')
        return

    case = match_case(extraction)
    if not case:
        send_message(chat_id,
                     f'Could not find a matching case with:\n'
                     f'{extraction.case_type or "?"}/{extraction.case_number or "?"}/{extraction.case_year or "?"} — {extraction.party_1 or "?"} vs {extraction.party_2 or "?"}\n\n'
                     f'Please check the case details and try again.')
        return

    entry = create_entry_from_extraction(extraction, case, advocate=profile.user)
    if not entry:
        send_message(chat_id, 'Failed to create diary entry. Please ensure a next date was provided.')
        return

    prev = entry.previous_date.strftime('%d-%m-%Y')
    nxt = entry.next_date.strftime('%d-%m-%Y')
    user_name = profile.user.get_full_name() or profile.user.username

    entry_message = (
        f'✅ New diary entry by <b>{user_name}</b>\n\n'
        f'<b>Case:</b> {case.case_type} {case.case_number}/{case.case_year}\n'
        f'<b>Parties:</b> {case.party_1} vs {case.party_2}\n'
        f'<b>Appearance:</b> {prev}\n'
        f'<b>Next Date:</b> {nxt}\n'
        f'<b>Business:</b> {entry.business}\n'
        f'<b>Stage:</b> {entry.stage or "—"}'
    )
    send_group_message(entry_message)
    send_message(chat_id,
                 f'✅ Diary entry created for <b>{case.case_type} {case.case_number}/{case.case_year}</b>!\n\n'
                 f'<b>Appearance:</b> {prev}\n'
                 f'<b>Next Date:</b> {nxt}\n'
                 f'<b>Business:</b> {entry.business}\n'
                 f'<b>Stage:</b> {entry.stage or "—"}\n\n'
                 f'You can view/edit it on the website.')

    if extraction.wants_reminder is False:
        send_message(chat_id, '✅ No reminder will be set (as requested).')
        return

    if extraction.wants_reminder is True:
        set_state(chat_id, 'AWAITING_REMINDER_DETAILS',
                  diary_entry_id=entry.id,
                  next_date_str=nxt)
        send_message(chat_id,
                     f'Now, please tell me the reminder details in one message:\n\n'
                     f'1. Task description?\n'
                     f'2. Frequency? (daily / alternate / twice_week / weekly)\n'
                     f'3. Enable ramp-up (daily last week)? (yes/no)\n'
                     f'4. Start date? (DD-MM-YYYY, default today)')
        return

    set_state(chat_id, 'AWAITING_REMINDER_YESNO',
              diary_entry_id=entry.id,
              next_date_str=nxt)
    send_message(chat_id, 'Would you like to set a reminder for this case? (yes/no)')


def process_update(update):
    """Process a single Telegram update (message). Used by both webhook and polling."""
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return

    if msg['chat'].get('type') in ('group', 'supergroup'):
        return

    chat_id = str(msg['chat']['id'])
    text = msg.get('text', '').strip()
    first_name = msg['chat'].get('first_name', '')
    if not text:
        return

    profile = UserProfile.objects.filter(telegram_chat_id=chat_id).first()

    # ── /start command ──
    if text == '/start':
        if profile:
            send_message(chat_id, f'Hi {first_name}! You are already linked as <b>{profile.user.username}</b>.')
        else:
            send_message(chat_id,
                         f'Hello {first_name}! Send your 6-digit verification code to link your SLA E-Diary account.')
        return

    # ── Not linked → try code ──
    if not profile:
        if len(text) == 6 and text.isdigit():
            code = text
            profile = UserProfile.objects.filter(telegram_code=code).first()
            if not profile:
                send_message(chat_id, 'Invalid code. Please check and try again.')
                return
            profile.telegram_chat_id = chat_id
            profile.telegram_code = None
            profile.save()
            send_message(chat_id,
                         f'✅ Linked as <b>{profile.user.username}</b>! You can now send diary entry updates.')
        else:
            send_message(chat_id,
                         'Please send your 6-digit verification code to link your account, or type /start to begin.')
        return

    # ── Linked user — check conversation state ──
    state = get_state(chat_id)
    current_state = state.get('state', 'IDLE')

    if current_state == 'AWAITING_REMINDER_YESNO':
        handle_reminder_yesno(chat_id, text, state)
        return

    if current_state == 'AWAITING_REMINDER_DETAILS':
        handle_reminder_details(chat_id, text, state)
        return

    # IDLE — classify and process
    try:
        classification = classify_message(text)
    except Exception as e:
        logger.exception(f'Classification failed: {e}')
        send_message(chat_id, 'Sorry, I had trouble processing that message. Please try again.')
        return

    if not classification.is_diary_entry:
        send_message(chat_id, 'I can only process diary entry updates. Please send a message with case details (case type, number, year, and what happened in court).')
        return

    handle_ai_diary_entry(chat_id, text, profile)
