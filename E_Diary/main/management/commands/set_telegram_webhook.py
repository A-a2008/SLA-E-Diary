import os
import requests

from django.core.management.base import BaseCommand
from dotenv import load_dotenv

load_dotenv()

from main.telegram_utils import set_webhook, delete_webhook


class Command(BaseCommand):
    help = 'Manage Telegram bot webhook'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['set', 'delete', 'info'])
        parser.add_argument('--url', help='Webhook URL (required for set)')

    def handle(self, *args, **options):
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            self.stdout.write(self.style.ERROR('TELEGRAM_BOT_TOKEN not set in .env'))
            return

        action = options['action']
        if action == 'set':
            url = options.get('url')
            if not url:
                self.stdout.write(self.style.ERROR('--url is required for set action'))
                return
            set_webhook(url)
            self.stdout.write(self.style.SUCCESS(f'Webhook set to {url}'))
        elif action == 'delete':
            delete_webhook()
            self.stdout.write(self.style.SUCCESS('Webhook deleted'))
        elif action == 'info':
            r = requests.get(f'https://api.telegram.org/bot{token}/getWebhookInfo', timeout=10)
            self.stdout.write(str(r.json()))
