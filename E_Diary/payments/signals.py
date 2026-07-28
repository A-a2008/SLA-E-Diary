import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User, Group

from main.models import DiaryEntry
from .models import EntryClassification
from .services import classify_and_setup, sync_invoice

logger = logging.getLogger(__name__)


@receiver(post_save, sender=DiaryEntry)
def auto_classify_entry(sender, instance, created, **kwargs):
    if getattr(instance, '_skip_payments', False):
        return
    if instance.entry_type not in ('business', 'mediation'):
        return
    try:
        old = DiaryEntry.objects.filter(pk=instance.pk).values('business', 'ecourts_business').first()
        if old:
            biz_changed = (old['business'] or '') != (instance.business or '')
            ec_changed = (old['ecourts_business'] or '') != (instance.ecourts_business or '')
            if not biz_changed and not ec_changed and not created:
                return
        classify_and_setup(instance)
    except Exception as e:
        logger.error(f"Auto-classify failed for entry {instance.pk}: {e}", exc_info=True)


@receiver(post_save, sender=EntryClassification)
def sync_invoice_on_classify(sender, instance, **kwargs):
    try:
        sync_invoice(instance)
    except Exception as e:
        logger.error(f"Sync invoice failed for classification {instance.pk}: {e}", exc_info=True)


@receiver(post_save, sender=User)
def ensure_payments_group(sender, instance, created, **kwargs):
    Group.objects.get_or_create(name='payments')
