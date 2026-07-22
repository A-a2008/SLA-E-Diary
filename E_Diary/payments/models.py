from django.db import models
from django.contrib.auth.models import User, Group
from django.db.models.signals import post_save
from django.dispatch import receiver

from main.models import Case, DiaryEntry


class ChargeType(models.Model):
    BOTH = 'both'
    PETITIONER = 'petitioner'
    RESPONDENT = 'respondent'
    APPLIES_TO_CHOICES = [
        (BOTH, 'Both'),
        (PETITIONER, 'Petitioner'),
        (RESPONDENT, 'Respondent'),
    ]
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    applies_to = models.CharField(max_length=20, choices=APPLIES_TO_CHOICES, default=BOTH)
    requires_cc_criminal = models.BooleanField(default=False)
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ['position', 'name']

    def __str__(self):
        return self.name


class CasePricing(models.Model):
    case = models.OneToOneField(Case, on_delete=models.CASCADE, related_name='pricing')
    client_phone = models.CharField(max_length=20, blank=True, default='')
    client_name = models.CharField(max_length=200, blank=True, default='')
    is_one_time = models.BooleanField(default=False)
    one_time_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    appearance_included = models.BooleanField(default=False)
    appearance_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    fully_paid = models.BooleanField(default=False)
    fully_paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Pricing for {self.case}"


class CaseChargeAmount(models.Model):
    case_pricing = models.ForeignKey(CasePricing, on_delete=models.CASCADE, related_name='charge_amounts')
    charge_type = models.ForeignKey(ChargeType, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ['case_pricing', 'charge_type']

    def __str__(self):
        return f"{self.charge_type.name}: {self.amount or 0}"


class CustomCharge(models.Model):
    case_pricing = models.ForeignKey(CasePricing, on_delete=models.CASCADE, related_name='custom_charges')
    name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.name}: {self.amount or 0}"


class OneTimeExtra(models.Model):
    case_pricing = models.ForeignKey(CasePricing, on_delete=models.CASCADE, related_name='one_time_extras')
    name = models.CharField(max_length=200)
    included_in_one_time = models.BooleanField(default=True)
    per_occurrence_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.name}: {'included' if self.included_in_one_time else str(self.per_occurrence_amount or 0)}"


class EntryClassification(models.Model):
    diary_entry = models.OneToOneField(DiaryEntry, on_delete=models.CASCADE, related_name='classification')
    classified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    classified_at = models.DateTimeField(auto_now_add=True)
    auto_classified = models.BooleanField(default=True)

    def __str__(self):
        return f"Classification for entry #{self.diary_entry_id}"


class EntryChargeItem(models.Model):
    entry_classification = models.ForeignKey(EntryClassification, on_delete=models.CASCADE, related_name='charge_items')
    charge_type = models.ForeignKey(ChargeType, on_delete=models.SET_NULL, null=True, blank=True)
    custom_charge_name = models.CharField(max_length=200, blank=True, default='')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        label = self.charge_type.name if self.charge_type else self.custom_charge_name
        return f"{label}: {self.amount}"


class DiaryEntryPayment(models.Model):
    diary_entry = models.OneToOneField(DiaryEntry, on_delete=models.CASCADE, related_name='payment_info')
    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Payment for entry #{self.diary_entry_id}: {'PAID' if self.is_paid else 'UNPAID'}"
