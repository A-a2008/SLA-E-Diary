from django.urls import path
from .views import *

urlpatterns = [
    path('', home, name='home'),
    path('new_case/', new_case, name='new_case'),
    path('batch_new_case/', batch_new_case, name='batch_new_case'),
    path('diary_entry/', diary_entry, name='diary_entry'),
    path('diary_entry_case/<int:case_id>/', diary_entry_case, name='diary_entry_case'),
    path('diary_entry_case/<int:case_id>/add_business/', add_business, name='add_business'),
    path('case/<int:case_id>/export/docx/', case_export_docx, name='case_export_docx'),
    path('case/<int:case_id>/export/pdf/', case_export_pdf, name='case_export_pdf'),
    path('edit_business/<int:entry_id>/', edit_business, name='edit_business'),
    path('cause_list/', cause_list, name='cause_list'),
    path('cause_list/docx/', cause_list_docx, name='cause_list_docx'),
    path('cause_list/pdf/', cause_list_pdf, name='cause_list_pdf'),
    path('search/', case_search, name='search_cases'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('change_password/', change_password, name='change_password'),
    path('manage/', super_dashboard, name='super_dashboard'),
    path('manage/admins/create/', super_create_admin, name='super_create_admin'),
    path('manage/users/', manage_users, name='manage_users'),
    path('manage/users/create/', admin_create_user, name='admin_create_user'),
    path('manage/users/<int:user_id>/toggle/', toggle_user_active, name='toggle_user_active'),
    path('manage/users/<int:user_id>/reset_password/', admin_reset_password, name='admin_reset_password'),
]
