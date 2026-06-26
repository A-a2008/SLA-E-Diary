import datetime
import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from main.models import Reminder
from main.telegram_utils import send_group_message

logger = logging.getLogger(__name__)


def is_reminder_due_today(reminder, today):
    if reminder.completed:
        return False

    if today < reminder.start_on:
        return False

    next_date = reminder.diary_entry.next_date
    if today > next_date:
        reminder.completed = True
        reminder.save()
        return False

    if reminder.last_sent_at and reminder.last_sent_at.date() == today:
        return False

    if reminder.ramp_up and 0 <= (next_date - today).days <= 7:
        return True

    days_since_start = (today - reminder.start_on).days
    if reminder.frequency == 'daily':
        return True
    elif reminder.frequency == 'alternate':
        return days_since_start % 2 == 0
    elif reminder.frequency == 'twice_week':
        return today.weekday() in (0, 3)
    elif reminder.frequency == 'weekly':
        return today.weekday() == reminder.start_on.weekday()

    return False


def format_reminder_message(reminder):
    entry = reminder.diary_entry
    case = entry.case

    lines = [
        '<b>📋 Reminder</b>',
        '',
        f'<b>Case:</b> {case.case_type} {case.case_number}/{case.case_year}',
        f'<b>Parties:</b> {"<b>" if case.represents_party_1 else ""}{case.party_1}{"</b>" if case.represents_party_1 else ""} vs {"<b>" if case.represents_party_2 else ""}{case.party_2}{"</b>" if case.represents_party_2 else ""}',
        f'<b>Task:</b> {reminder.task}',
        f'<b>Next Hearing:</b> {entry.next_date.strftime("%d %b %Y")}',
        f'<b>Frequency:</b> {reminder.get_frequency_display()}',
    ]

    if reminder.ramp_up:
        lines.append('<b>Ramp-up:</b> Enabled (daily in last week)')

    advocate = entry.advocate
    if advocate:
        lines.append(f'<b>Advocate:</b> {advocate.get_full_name() or advocate.username}')

    return '\n'.join(lines)


def is_auto_time():
    now = timezone.localtime(timezone.now())
    return now.hour >= 18 and now.minute >= 30


def send_due_reminders(dry_run=False, auto=False):
    """Send due reminders.

    Args:
        dry_run: If True, just log what would be sent.
        auto: If True (called from the auto-scheduler at 6:30 PM),
              only sends at/after 18:30 IST and updates last_sent_at.
              If False (manual command), sends immediately and does NOT
              update last_sent_at so the 6:30 auto-send still works.
    """
    today = datetime.date.today()
    sent = 0

    if auto and not is_auto_time():
        logger.info('Auto-reminder skipped: before 6:30 PM IST')
        return 0

    reminders = Reminder.objects.select_related(
        'diary_entry__case', 'diary_entry__advocate'
    ).filter(completed=False)

    for reminder in reminders:
        if not is_reminder_due_today(reminder, today):
            continue

        message = format_reminder_message(reminder)
        if dry_run:
            logger.info(f'[DRY RUN] Would send reminder #{reminder.id}: {reminder.task}')
            sent += 1
        else:
            ok = send_group_message(message)
            if ok:
                if auto:
                    reminder.last_sent_at = timezone.now()
                    reminder.save(update_fields=['last_sent_at'])
                sent += 1
            else:
                logger.warning(f'Failed to send reminder #{reminder.id}, will retry next cycle')

    return sent


class Command(BaseCommand):
    help = 'Send due reminders to the Telegram group'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Check what would be sent without sending')
        parser.add_argument('--reset', action='store_true', help='Clear last_sent_at on all reminders (so they send again)')
        parser.add_argument('--auto', action='store_true', help='Used by auto-scheduler; only sends at 6:30 PM IST and updates last_sent_at')

    def handle(self, *args, **options):
        if options.get('reset'):
            count = Reminder.objects.filter(last_sent_at__isnull=False).update(last_sent_at=None)
            self.stdout.write(self.style.SUCCESS(f'Reset last_sent_at on {count} reminder(s)'))
            return
        dry_run = options.get('dry_run')
        auto = options.get('auto')
        sent = send_due_reminders(dry_run=dry_run, auto=auto)
        if dry_run:
            self.stdout.write(f'{sent} reminder(s) ready to send (dry run)')
        else:
            verb = 'auto-sent' if auto else 'sent'
            self.stdout.write(self.style.SUCCESS(f'{sent} reminder(s) {verb} to group'))
