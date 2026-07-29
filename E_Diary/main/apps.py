from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _ensure_delete_journal(sender, connection, **kwargs):
    """Force single-file journal mode — prevents WAL side-files that corrupt on PythonAnywhere."""
    if connection.vendor == 'sqlite':
        try:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA journal_mode=DELETE;')
        except Exception:
            pass


class MainConfig(AppConfig):
    name = 'main'

    def ready(self):
        connection_created.connect(_ensure_delete_journal)
