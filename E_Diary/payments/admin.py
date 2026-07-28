from django.contrib import admin

from .models import (
    ChargeType, CasePricing, CaseChargeAmount, CustomCharge,
    OneTimeExtra, EntryClassification, EntryChargeItem, DiaryEntryPayment,
    Client, CaseClient, Invoice, Transaction, TransactionCase
)


@admin.register(ChargeType)
class ChargeTypeAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'applies_to', 'requires_cc_criminal', 'position']
    list_editable = ['position']


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone', 'created_at']
    search_fields = ['name', 'phone']


@admin.register(CaseClient)
class CaseClientAdmin(admin.ModelAdmin):
    list_display = ['case', 'client']


@admin.register(CasePricing)
class CasePricingAdmin(admin.ModelAdmin):
    list_display = ['case', 'client_name', 'client_phone', 'is_one_time', 'fully_paid']


@admin.register(CaseChargeAmount)
class CaseChargeAmountAdmin(admin.ModelAdmin):
    list_display = ['case_pricing', 'charge_type', 'amount']


@admin.register(CustomCharge)
class CustomChargeAdmin(admin.ModelAdmin):
    list_display = ['case_pricing', 'name', 'amount']


@admin.register(OneTimeExtra)
class OneTimeExtraAdmin(admin.ModelAdmin):
    list_display = ['case_pricing', 'name', 'included_in_one_time', 'per_occurrence_amount']


@admin.register(EntryClassification)
class EntryClassificationAdmin(admin.ModelAdmin):
    list_display = ['diary_entry', 'auto_classified', 'classified_at']


@admin.register(EntryChargeItem)
class EntryChargeItemAdmin(admin.ModelAdmin):
    list_display = ['entry_classification', 'charge_type', 'amount']


@admin.register(DiaryEntryPayment)
class DiaryEntryPaymentAdmin(admin.ModelAdmin):
    list_display = ['diary_entry', 'is_paid', 'paid_at', 'paid_by']


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_no', 'case', 'client', 'amount', 'created_at']
    search_fields = ['invoice_no']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['client', 'amount', 'payment_method', 'transaction_date']
    search_fields = ['client__name']


@admin.register(TransactionCase)
class TransactionCaseAdmin(admin.ModelAdmin):
    list_display = ['transaction', 'case']
