from django.db import models
from django.contrib.auth.models import User
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


class Client(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    address = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.phone})"


class CaseClient(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='case_clients')
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='case_clients')

    class Meta:
        unique_together = ('case', 'client')

    def __str__(self):
        return f"{self.client.name} → {self.case}"


class CasePricing(models.Model):
    case = models.OneToOneField(Case, on_delete=models.CASCADE, related_name='pricing')
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
    invoice_message = models.TextField(blank=True, default='',
        help_text='Custom message for the invoice (overrides business text)')

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


class Invoice(models.Model):
    invoice_no = models.CharField(max_length=50, unique=True)
    diary_entry = models.OneToOneField(DiaryEntry, on_delete=models.CASCADE, related_name='invoice')
    client = models.ForeignKey(Client, on_delete=models.CASCADE, null=True, blank=True)
    case = models.ForeignKey(Case, on_delete=models.CASCADE)
    particulars = models.TextField(blank=True, default='')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Invoice {self.invoice_no} – {self.amount}"


class Transaction(models.Model):
    PAYMENT_CASH = 'cash'
    PAYMENT_UPI = 'upi'
    PAYMENT_BANK = 'bank'
    PAYMENT_CHEQUE = 'cheque'
    PAYMENT_CARD = 'card'
    PAYMENT_DD = 'dd'
    PAYMENT_OTHER = 'other'
    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_CASH, 'Cash'),
        (PAYMENT_UPI, 'UPI'),
        (PAYMENT_BANK, 'Bank Transfer'),
        (PAYMENT_CHEQUE, 'Cheque'),
        (PAYMENT_CARD, 'Card'),
        (PAYMENT_DD, 'DD'),
        (PAYMENT_OTHER, 'Other'),
    ]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    other_method_detail = models.CharField(max_length=100, blank=True, default='')
    transaction_date = models.DateTimeField()
    transaction_no = models.CharField(max_length=50, unique=True, blank=True, null=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-transaction_date']

    def __str__(self):
        return f"{self.get_payment_method_display()} {self.amount} – {self.client.name}"


class TransactionCase(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='cases')
    case = models.ForeignKey(Case, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('transaction', 'case')

    def __str__(self):
        return f"{self.transaction} → {self.case}"
