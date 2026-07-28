from django.urls import path
from . import views

app_name = 'payments'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('due/', views.payments_due, name='payments_due'),
    path('cases/', views.case_list, name='case_list'),
    path('cases/<int:case_id>/', views.case_pricing, name='case_pricing'),
    path('cases/<int:case_id>/entries/', views.case_entries, name='case_entries'),
    path('cases/<int:case_id>/mark-full-paid/', views.mark_full_paid, name='mark_full_paid'),
    path('cases/<int:case_id>/fee-agreement/pdf/', views.fee_agreement_pdf, name='fee_agreement_pdf'),
    path('cases/<int:case_id>/fee-agreement/image/', views.fee_agreement_image, name='fee_agreement_image'),
    path('cases/<int:case_id>/clients/', views.case_clients, name='case_clients'),
    path('cases/<int:case_id>/statement/', views.case_statement, name='case_statement'),
    path('cases/<int:case_id>/transactions/add/', views.add_transaction, name='add_transaction'),
    path('cases/<int:case_id>/refresh-amounts/', views.refresh_amounts, name='refresh_amounts'),
    path('clients/', views.client_list, name='client_list'),
    path('clients/add/', views.client_add, name='client_add'),
    path('clients/<int:client_id>/edit/', views.client_edit, name='client_edit'),
    path('clients/<int:client_id>/statement/', views.client_statement, name='client_statement'),
    path('clients/<int:client_id>/transactions/add/', views.client_add_transaction, name='client_add_transaction'),
    path('entries/<int:entry_id>/classify/', views.edit_classification, name='edit_classification'),
    path('entries/<int:entry_id>/payment/', views.toggle_payment, name='toggle_payment'),
    path('entries/<int:entry_id>/quick-classify/', views.quick_classify, name='quick_classify'),
    path('entries/<int:entry_id>/invoice/pdf/', views.invoice_pdf, name='invoice_pdf'),
    path('entries/<int:entry_id>/invoice/image/', views.invoice_image, name='invoice_image'),
    path('entries/batch-pay/', views.batch_pay, name='batch_pay'),
    path('reclassify/<int:entry_id>/', views.reclassify_single, name='reclassify_single'),
    path('reclassify-case/<int:case_id>/', views.reclassify_case, name='reclassify_case'),
    path('transactions/', views.transaction_list, name='transaction_list'),
    path('invoices/', views.invoice_list, name='invoice_list'),
    path('users/', views.manage_payments_users, name='manage_payments_users'),
    path('users/add/', views.add_payments_user, name='add_payments_user'),
    path('users/<int:user_id>/remove/', views.remove_payments_user, name='remove_payments_user'),
]
