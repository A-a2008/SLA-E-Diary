from django.db import migrations, models
import django.db.models.deletion


def backfill_null_clients(apps, schema_editor):
    Invoice = apps.get_model('payments', 'Invoice')
    Client = apps.get_model('payments', 'Client')
    CaseClient = apps.get_model('payments', 'CaseClient')
    for inv in Invoice.objects.filter(client__isnull=True).select_related('case'):
        cc = CaseClient.objects.filter(case=inv.case).first()
        if cc:
            inv.client = cc.client
            inv.save(update_fields=['client'])
        else:
            first = Client.objects.first()
            if not first:
                first = Client.objects.create(name='Migrated Client', phone='0000000000')
            inv.client = first
            inv.save(update_fields=['client'])


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0026_causelistentry_mediation_time'),
        ('payments', '0006_invoice_invoice_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='invoice_message',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.RunPython(backfill_null_clients, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='invoice',
            name='client',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='payments.client'),
        ),
        migrations.AlterField(
            model_name='invoice',
            name='diary_entry',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoices', to='main.diaryentry'),
        ),
        migrations.AlterUniqueTogether(
            name='invoice',
            unique_together={('diary_entry', 'client')},
        ),
    ]
