from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'

    def ready(self):
        import payments.signals
        self._seed_charge_types()

    def _seed_charge_types(self):
        try:
            from django.db import transaction
            from django.db.utils import OperationalError
            from .models import ChargeType
            # First, rename existing 'mediation' to 'mediation_attended'
            ChargeType.objects.filter(code='mediation').update(
                code='mediation_attended', name='Mediation Attended'
            )
            charges = [
                ('hearing', 'Hearing/Appearance', 'both', False, 1),
                ('evidence_chief', 'Evidence: Chief', 'both', False, 2),
                ('evidence_cross', 'Evidence: Cross Examination', 'both', False, 3),
                ('arguments', 'Arguments', 'both', False, 4),
                ('mediation_attended', 'Mediation Attended', 'both', False, 5),
                ('filing_ep', 'Filing EP', 'both', True, 6),
                ('ia', 'IA', 'both', False, 7),
                ('ia_objections', 'IA Objections', 'both', False, 8),
                ('ia_hearing', 'IA Hearing', 'both', False, 9),
                ('preparation', 'Preparation', 'petitioner', False, 10),
                ('filing', 'Filing', 'petitioner', False, 11),
                ('filing_vakalat', 'Filing Vakalat', 'respondent', False, 12),
                ('filing_objections', 'Filing Objections', 'respondent', False, 13),
            ]
            with transaction.atomic():
                existing = set(ChargeType.objects.values_list('code', flat=True))
                for code, name, applies_to, req_cc, pos in charges:
                    if code not in existing:
                        ChargeType.objects.create(
                            code=code, name=name,
                            applies_to=applies_to,
                            requires_cc_criminal=req_cc,
                            position=pos,
                        )
        except Exception:
            pass  # table doesn't exist yet (first migration) or other issues
