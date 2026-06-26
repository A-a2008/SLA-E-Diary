#!/usr/bin/env python3
"""
Standalone Telegram bot for SLA E-Diary.

Run this alongside the Django server to enable Telegram alerts.
Usage:
    python telegram_bot.py
"""

import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'E_Diary.settings')

import django
django.setup()

from dotenv import load_dotenv
load_dotenv()

from main.management.commands.run_telegram_bot import run_polling

if __name__ == '__main__':
    print('SLA E-Diary Telegram Bot')
    print('Press Ctrl+C to stop')
    try:
        run_polling()
    except KeyboardInterrupt:
        print('\nBot stopped.')
