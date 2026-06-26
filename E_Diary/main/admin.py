from django.contrib import admin
from .models import *

# Register your models here.

@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ('case_type', 'case_number', 'case_year', 'party_1', 'party_2', 'court', 'disposed')
    list_filter = ('disposed', 'court_level', 'jurisdiction')
    search_fields = ('case_number', 'party_1', 'party_2', 'case_type')

@admin.register(DiaryEntry)
class DiaryEntryAdmin(admin.ModelAdmin):
    list_display = ('case', 'previous_date', 'next_date', 'stage', 'advocate', 'created_at')
    list_filter = ('stage', 'previous_date', 'next_date')
    search_fields = ('case__case_number', 'case__party_1', 'case__party_2', 'business')

@admin.register(CauseListEntry)
class CauseListEntryAdmin(admin.ModelAdmin):
    list_display = ('date', 'case', 'list_i', 'list_ii')
    list_filter = ('date',)
    search_fields = ('case__case_number',)

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'phone')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
