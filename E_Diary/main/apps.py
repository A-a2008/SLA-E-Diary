from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _activate_sqlite_wal(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        try:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA journal_mode=WAL;')
                cursor.execute('PRAGMA busy_timeout=5000;')
                cursor.execute('PRAGMA synchronous=NORMAL;')
        except Exception:
            pass


class MainConfig(AppConfig):
    name = 'main'

    def ready(self):
        connection_created.connect(_activate_sqlite_wal)
