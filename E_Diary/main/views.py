import json
import re
import datetime
import logging
from io import BytesIO

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from django.utils.crypto import get_random_string
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Party1Type, Party2Type, Jurisdiction, CourtLevel, MediationStatus, MediationEntryType, Case, DiaryEntry, CauseListEntry, UserProfile, UserRole, CourtHallNote, Reminder
from .constants import COURT_LABELS, BUILDING_LABELS, BUILDING_ORDER, COURT_TO_BUILDING
from .services import search_cases, get_latest_entry_data, create_diary_entry, create_case, dispose_case, reinstate_case
from .telegram_utils import send_message

logger = logging.getLogger(__name__)



# ── ADMIN / SUPERUSER USER MANAGEMENT ──

@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def admin_create_user(request):
    admin_roles = [r for r in UserRole.choices if r[0] != UserRole.ADMIN]
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        first_name = request.POST.get('first_name', '')
        last_name = request.POST.get('last_name', '')
        role = request.POST.get('role', UserRole.INTERN)
        phone = request.POST.get('phone', '')
        if User.objects.filter(username=username).exists():
            return render(request, 'registration/admin_create_user.html', {
                'error': 'Username already exists.',
                'role_choices': admin_roles,
            })
        user = User.objects.create_user(username=username, password=password, first_name=first_name, last_name=last_name)
        UserProfile.objects.create(user=user, role=role, phone=phone)
        messages.success(request, f'User "{username}" ({role}) created successfully.')
        return redirect('admin_create_user')
    return render(request, 'registration/admin_create_user.html', {
        'role_choices': admin_roles,
    })


@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def manage_users(request):
    if request.user.is_superuser:
        users = User.objects.all().select_related('userprofile').order_by('username')
    else:
        users = User.objects.exclude(is_superuser=True).exclude(userprofile__role=UserRole.ADMIN).select_related('userprofile').order_by('username')
    return render(request, 'registration/manage_users.html', {'users': users})


@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def toggle_user_active(request, user_id):
    user = get_object_or_404(User, id=user_id)
    if not request.user.is_superuser and (user.is_superuser or (hasattr(user, 'userprofile') and user.userprofile.role == UserRole.ADMIN)):
        messages.error(request, 'You cannot suspend another admin.')
        return redirect('manage_users')
    user.is_active = not user.is_active
    user.save()
    if hasattr(user, 'userprofile'):
        profile = user.userprofile
        if not user.is_active:
            profile.left_on = datetime.date.today()
        else:
            profile.left_on = None
        profile.save()
    messages.success(request, f'User "{user.username}" {"activated" if user.is_active else "suspended"}.')
    return redirect('manage_users')


@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def admin_reset_password(request, user_id):
    user = get_object_or_404(User, id=user_id)
    if not request.user.is_superuser and (user.is_superuser or (hasattr(user, 'userprofile') and user.userprofile.role == UserRole.ADMIN)):
        messages.error(request, 'You cannot reset another admin\'s password.')
        return redirect('manage_users')
    new_password = get_random_string(length=12)
    user.set_password(new_password)
    user.save()
    messages.success(request, f'Password for "{user.username}" reset to: {new_password}')
    return redirect('manage_users')


@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def user_detail(request, user_id):
    user = get_object_or_404(User, id=user_id)
    if not request.user.is_superuser and (user.is_superuser or (hasattr(user, 'userprofile') and user.userprofile.role == UserRole.ADMIN)):
        messages.error(request, 'You cannot view another admin\'s details.')
        return redirect('manage_users')
    try:
        profile = user.userprofile
    except UserProfile.DoesNotExist:
        profile = None
    return render(request, 'registration/user_detail.html', {
        'profile_user': user,
        'profile': profile,
    })


@login_required
@user_passes_test(lambda u: hasattr(u, 'userprofile') and u.userprofile.role == UserRole.ADMIN or u.is_superuser)
def regenerate_telegram_code(request, user_id):
    user = get_object_or_404(User, id=user_id)
    try:
        profile = user.userprofile
    except UserProfile.DoesNotExist:
        messages.error(request, 'User has no profile.')
        return redirect('manage_users')

    unlink = request.GET.get('unlink')
    if profile.telegram_chat_id and not unlink:
        messages.warning(request, f'"{user.username}" already linked to Telegram. Use "Unlink & Regenerate" first.')
    else:
        if profile.telegram_chat_id:
            profile.telegram_chat_id = None
        profile.telegram_code = UserProfile._generate_code()
        profile.save()
        messages.success(request, f'New Telegram code for "{user.username}": {profile.telegram_code}')

    return redirect('user_detail', user_id=user.id)


# ── SUPERUSER PORTAL ──

@login_required
@user_passes_test(lambda u: u.is_superuser)
def super_dashboard(request):
    return render(request, 'registration/super_dashboard.html')


@login_required
@user_passes_test(lambda u: u.is_superuser)
def super_create_admin(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        first_name = request.POST.get('first_name', '')
        last_name = request.POST.get('last_name', '')
        phone = request.POST.get('phone', '')
        if User.objects.filter(username=username).exists():
            return render(request, 'registration/super_create_admin.html', {
                'error': 'Username already exists.',
            })
        user = User.objects.create_user(username=username, password=password, first_name=first_name, last_name=last_name)
        user.is_staff = True
        user.save()
        UserProfile.objects.create(user=user, role=UserRole.ADMIN, phone=phone)
        messages.success(request, f'Admin "{username}" created successfully.')
        return redirect('super_dashboard')
    return render(request, 'registration/super_create_admin.html')


def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get('next', 'home')
            return redirect(next_url)
        else:
            return render(request, 'registration/login.html', {'error': 'Invalid username or password.'})
    return render(request, 'registration/login.html')


@login_required
def logout_view(request):
    logout(request)
    return redirect('home')


@login_required
def change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, 'Password changed successfully.')
            return redirect('home')
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'registration/change_password.html', {'form': form})


# ── CASE ──

@login_required
def new_case(request):
    if request.method == 'POST':
        court_level = request.POST.get('court_level')
        jurisdiction = request.POST.get('jurisdiction')
        court = request.POST.get('court')
        court_hall = request.POST.get('court_hall')
        case_type = request.POST.get('case_type')
        case_number = request.POST.get('case_number')
        party_1 = request.POST.get('party_1')
        party_1_type = request.POST.get('party_1_type')
        party_2 = request.POST.get('party_2')
        party_2_type = request.POST.get('party_2_type')
        representing = request.POST.get('representing')

        floor = int(request.POST.get('floor') or 0)
        case_year = int(request.POST.get('case_year') or 2024)

        party_1_total = int(request.POST.get('party_1_total') or 1)
        party_2_total = int(request.POST.get('party_2_total') or 1)

        safe_rep = re.sub(r'\W+', '_', representing.lower()).strip('_')
        representing_party_field = f'representing_{safe_rep}_indices'
        representing_parties_list = request.POST.getlist(representing_party_field)
        if representing_parties_list:
            representing_parties = ','.join(representing_parties_list)
        else:
            representing_parties = request.POST.get('representing_parties', '1')

        case = create_case(
            jurisdiction=jurisdiction, court_level=court_level, court=court,
            court_hall=court_hall, floor=floor, case_type=case_type,
            case_number=case_number, case_year=case_year, party_1=party_1,
            party_1_type=party_1_type, party_2=party_2, party_2_type=party_2_type,
            representing=representing,
            representing_parties=representing_parties,
            party_1_total=party_1_total,
            party_2_total=party_2_total,
        )

        CourtHallNote.objects.get_or_create(court=court, court_hall=court_hall, defaults={'note': ''})

        return redirect("diary_entry")
    else:
        data = {
            'party1_choices': Party1Type.choices,
            'party2_choices': Party2Type.choices,
            'jurisdiction_choices': Jurisdiction.choices,
            'court_level_choices': CourtLevel.choices,
        }
        return render(request, 'main/new_case.html', data)


@login_required
def diary_entry(request):
    query = request.GET.get('q', '').strip()
    court_level = request.GET.get('court_level', '')
    disposed_filter = request.GET.get('disposed', '')
    cases = search_cases(query=query, court_level=court_level, disposed=disposed_filter)

    today = datetime.date.today()
    case_list = list(cases)

    def sort_key(case):
        last_entry = case.diary_entries.order_by('-next_date').first()
        if last_entry is None:
            return (0, datetime.date.min, -case.id)
        nd = last_entry.next_date
        if nd >= today:
            return (1, nd, -case.id)
        return (2, nd, -case.id)

    case_list.sort(key=sort_key)

    for case in case_list:
        case.court_display_name = COURT_LABELS.get(case.court, case.court)
        last_entry = case.diary_entries.order_by('-next_date').first()
        if last_entry:
            case.next_date = last_entry.next_date
            case.prev_date = last_entry.previous_date
        else:
            case.next_date = None
            case.prev_date = None
    return render(request, 'main/diary_entry.html', {
        'today': today, 'cases': case_list, 'query': query, 'court_labels': COURT_LABELS,
        'court_level_choices': CourtLevel.choices,
        'selected_court_level': court_level, 'selected_disposed': disposed_filter,
    })


@login_required
def diary_entry_case(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    entries = case.diary_entries.all()
    latest_data = get_latest_entry_data(case)

    if request.method == 'POST':
        if request.POST.get('dispose_case'):
            dispose_case(case)
            return redirect('diary_entry_case', case_id=case.id)
        if request.POST.get('reinstate_case'):
            reinstate_case(case)
            return redirect('diary_entry_case', case_id=case.id)

    court_hall_notes = CourtHallNote.objects.filter(
        court=case.court, court_hall=case.court_hall
    )

    return render(request, 'main/diary_entry_case.html', {
        'case': case, 'entries': entries, 'court_labels': COURT_LABELS,
        'latest_data': latest_data, 'court_display': COURT_LABELS.get(case.court, case.court),
        'court_hall_notes': court_hall_notes,
        'mediation_statuses': MediationStatus,
    })


@login_required
def refer_to_mediation(request, case_id):
    case = get_object_or_404(Case, id=case_id)

    if request.method == 'POST':
        mediation_date = request.POST.get('mediation_date')
        notes = request.POST.get('notes', '').strip()

        if mediation_date:
            try:
                mediation_date_obj = datetime.datetime.strptime(mediation_date, '%Y-%m-%d').date()
            except ValueError:
                messages.error(request, 'Invalid date.')
                return redirect('diary_entry_case', case_id=case.id)

            case.mediation_status = MediationStatus.REFERRED
            case.mediation_next_date = mediation_date_obj
            case.save()

            business_text = 'Case referred to Karnataka Mediation Centre.'
            if notes:
                business_text += f'\n\nNotes: {notes}'

            create_diary_entry(
                case=case,
                entry_type='mediation',
                previous_date=datetime.date.today(),
                court='Karnataka Mediation Centre',
                court_hall='Mediation',
                floor=0,
                case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
                representing=case.representing,
                representing_parties=case.representing_parties,
                party_1_total=case.party_1_total,
                party_2_total=case.party_2_total,
                stage='Mediation',
                business=business_text,
                next_date=mediation_date_obj,
                advocate=request.user,
            )

            messages.success(request, f'Case referred to mediation. Next mediation date: {mediation_date}')
        else:
            messages.error(request, 'Please provide a mediation date.')

        return redirect('diary_entry_case', case_id=case.id)

    return render(request, 'main/refer_to_mediation.html', {
        'case': case, 'court_display': COURT_LABELS.get(case.court, case.court),
        'mediation_statuses': MediationStatus,
    })


@login_required
def update_mediation_status(request, case_id):
    case = get_object_or_404(Case, id=case_id)

    if request.method == 'POST':
        new_status = request.POST.get('mediation_status')
        mediation_date = request.POST.get('mediation_date')

        if new_status and new_status in [s.value for s in MediationStatus]:
            case.mediation_status = new_status
            if mediation_date:
                try:
                    case.mediation_next_date = datetime.datetime.strptime(mediation_date, '%Y-%m-%d').date()
                except ValueError:
                    pass
            elif new_status in (MediationStatus.SETTLED, MediationStatus.FAILED):
                case.mediation_next_date = None
            case.save()

            if new_status in (MediationStatus.SETTLED, MediationStatus.FAILED):
                label = dict(MediationStatus.choices).get(new_status, new_status)
                create_diary_entry(
                    case=case,
                    entry_type='mediation',
                    previous_date=datetime.date.today(),
                    court='Karnataka Mediation Centre',
                    court_hall='Mediation',
                    floor=0,
                    case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
                    representing=case.representing,
                    representing_parties=case.representing_parties,
                    party_1_total=case.party_1_total,
                    party_2_total=case.party_2_total,
                    stage='Mediation',
                    business=f'Mediation {label.lower()}.',
                    next_date=case.mediation_next_date or datetime.date.today(),
                    advocate=request.user,
                )

            messages.success(request, f'Mediation status updated to "{dict(MediationStatus.choices).get(new_status, new_status)}".')
        else:
            messages.error(request, 'Invalid status.')

        return redirect('diary_entry_case', case_id=case.id)


@login_required
def create_execution_case(request, case_id=None):
    original_case = None
    if case_id:
        original_case = get_object_or_404(Case, id=case_id)

    if request.method == 'POST':
        court = request.POST.get('court')
        court_hall = request.POST.get('court_hall', '')
        floor = request.POST.get('floor', 0)
        case_number = request.POST.get('case_number')
        case_year = request.POST.get('case_year')
        party_1 = request.POST.get('party_1')
        party_1_type = request.POST.get('party_1_type')
        party_2 = request.POST.get('party_2')
        party_2_type = request.POST.get('party_2_type')
        representing = request.POST.get('representing')
        jurisdiction = request.POST.get('jurisdiction')

        linked_case_id = request.POST.get('linked_case_id')
        linked_case = None
        if linked_case_id:
            try:
                linked_case = Case.objects.get(id=int(linked_case_id))
            except (ValueError, Case.DoesNotExist):
                pass

        try:
            case = Case.objects.create(
                jurisdiction=jurisdiction,
                court_level='district',
                court=court,
                court_hall=court_hall,
                floor=int(floor) if floor else 0,
                case_type='EX',
                case_number=case_number,
                case_year=int(case_year) if case_year else datetime.date.today().year,
                party_1=party_1,
                party_1_type=party_1_type,
                party_2=party_2,
                party_2_type=party_2_type,
                representing=representing,
                representing_parties='1',
                related_case=linked_case,
            )
            messages.success(request, f'Execution case created: EX/{case_number}/{case_year}')
            return redirect('diary_entry_case', case_id=case.id)
        except Exception as e:
            messages.error(request, f'Error creating case: {e}')

    today = datetime.date.today()
    party1_choices = Party1Type.choices
    party2_choices = Party2Type.choices

    # Pre-fill from original case if linked
    initial = {}
    if original_case:
        initial = {
            'court': original_case.court,
            'court_hall': original_case.court_hall,
            'floor': original_case.floor,
            'party_1': original_case.party_1,
            'party_1_type': original_case.party_1_type,
            'party_2': original_case.party_2,
            'party_2_type': original_case.party_2_type,
            'representing': original_case.representing,
            'jurisdiction': original_case.jurisdiction,
        }

    return render(request, 'main/create_execution_case.html', {
        'original_case': original_case,
        'initial': initial,
        'today': today,
        'party1_choices': party1_choices,
        'party2_choices': party2_choices,
        'urban_courts': [
            ('cmm', 'Chief Metropolitan Magistrate Court Complex, Bangalore'),
            ('mmtc', 'Metropolitan Magistrate Traffic Courts, Bangalore'),
            ('mmtc_mayo', 'Metropolitan Magistrate Traffic Court I, Mayo Hall, Bangalore'),
            ('city_civil', 'City Civil Court Complex, Bangalore'),
            ('family', 'Family Court Complex, Bangalore'),
            ('small_causes', 'Small Causes Court Complex, Bangalore'),
            ('mayo_hall', 'Mayo Hall Court Complex, Bangalore'),
            ('commercial', 'Commercial Court Complex, Bangalore'),
            ('consumer', 'Consumer Forum, Shantinagar'),
            ('urban_consumer', 'Urban Consumer Forum, Bangalore'),
        ],
        'rural_courts': [
            ('city_civil_rural', 'City Civil Court Complex, Bengaluru Rural'),
            ('dist_session_rural', 'PRL. District and Sessions Judge, Bengaluru Rural'),
            ('prl_senior_rural', 'PRL. Senior Civil Judge, Bengaluru Rural'),
            ('prl_junior_rural', 'PRL. Civil Judge, Bengaluru Rural'),
            ('cjm_cmm_rural', 'Chief Judicial Magistrate, Bengaluru Rural in CMM Court'),
            ('vacation_court_rural', 'Vacation Court, Bengaluru Rural'),
            ('commercial_court_rural', 'Commercial Court Complex, Bengaluru Rural'),
            ('labour_court_rural', 'Labour Court, Bengaluru Rural'),
            ('senior_anekal', 'Senior Civil Judge & JMFC, Anekal'),
            ('junior_anekal', 'PRL. Civil Judge & JMFC, Anekal'),
            ('hosakote', 'Court Complex – Hosakote'),
            ('devanahalli', 'Court Complex – Devanahalli'),
            ('doddaballapur', 'Court Complex – Doddaballapur'),
            ('nelamangala', 'Court Complex – Nelamangala'),
            ('kr_puram', 'KR Puram Court Complex'),
        ],
    })


@login_required
def add_business(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    latest_data = get_latest_entry_data(case)

    if request.method == 'POST':
        previous_date = request.POST.get('previous_date')
        court = COURT_LABELS.get(case.court, case.court)
        court_hall = case.court_hall
        floor = case.floor
        case_number_display = f"{case.case_type}/{case.case_number}/{case.case_year}"
        representing = request.POST.get('representing', case.representing)
        stage = request.POST.get('stage')
        business = request.POST.get('business')
        next_date = request.POST.get('next_date')

        safe_rep = re.sub(r'\W+', '_', representing.lower()).strip('_')
        representing_party_field = f'representing_{safe_rep}_indices'
        representing_parties_list = request.POST.getlist(representing_party_field)
        if representing_parties_list:
            representing_parties = ','.join(representing_parties_list)
        else:
            representing_parties = request.POST.get('representing_parties', case.representing_parties)

        party_1_total = int(request.POST.get('party_1_total') or case.party_1_total)
        party_2_total = int(request.POST.get('party_2_total') or case.party_2_total)

        entry = create_diary_entry(
            case=case, previous_date=previous_date, court=court,
            court_hall=court_hall, floor=floor,
            case_number_display=case_number_display, representing=representing,
            representing_parties=representing_parties,
            party_1_total=party_1_total,
            party_2_total=party_2_total,
            stage=stage, business=business, next_date=next_date,
            advocate=request.user,
        )

        if request.POST.get('needs_reminder'):
            reminder_task = request.POST.get('reminder_task', '').strip()
            reminder_start_on = request.POST.get('reminder_start_on')
            reminder_frequency = request.POST.get('reminder_frequency', 'daily')
            reminder_ramp_up = request.POST.get('reminder_ramp_up') == '1'
            if reminder_task and reminder_start_on:
                Reminder.objects.create(
                    diary_entry=entry,
                    task=reminder_task,
                    start_on=reminder_start_on,
                    frequency=reminder_frequency,
                    ramp_up=reminder_ramp_up,
                )

        return redirect('diary_entry_case', case_id=case.id)

    return render(request, 'main/add_business.html', {
        'case': case, 'latest_data': latest_data,
        'court_display': COURT_LABELS.get(case.court, case.court),
        'today': datetime.date.today(),
    })


@login_required
def edit_business(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)

    if request.method == 'POST':
        entry.previous_date = request.POST.get('previous_date')
        entry.court = COURT_LABELS.get(entry.case.court, entry.case.court)
        entry.court_hall = entry.case.court_hall
        entry.floor = entry.case.floor
        entry.case_number_display = f"{entry.case.case_type}/{entry.case.case_number}/{entry.case.case_year}"
        entry.representing = request.POST.get('representing', entry.case.representing)
        entry.stage = request.POST.get('stage')
        entry.business = request.POST.get('business')
        entry.next_date = request.POST.get('next_date')

        safe_rep = re.sub(r'\W+', '_', entry.representing.lower()).strip('_')
        representing_party_field = f'representing_{safe_rep}_indices'
        representing_parties_list = request.POST.getlist(representing_party_field)
        if representing_parties_list:
            entry.representing_parties = ','.join(representing_parties_list)

        party_1_total = request.POST.get('party_1_total')
        if party_1_total:
            entry.party_1_total = int(party_1_total)
        party_2_total = request.POST.get('party_2_total')
        if party_2_total:
            entry.party_2_total = int(party_2_total)

        entry.save()

        if request.POST.get('needs_reminder'):
            reminder_task = request.POST.get('reminder_task', '').strip()
            reminder_start_on = request.POST.get('reminder_start_on')
            reminder_frequency = request.POST.get('reminder_frequency', 'daily')
            reminder_ramp_up = request.POST.get('reminder_ramp_up') == '1'
            if reminder_task and reminder_start_on:
                reminder, created = Reminder.objects.get_or_create(
                    diary_entry=entry,
                    defaults={
                        'task': reminder_task,
                        'start_on': reminder_start_on,
                        'frequency': reminder_frequency,
                        'ramp_up': reminder_ramp_up,
                    }
                )
                if not created:
                    reminder.task = reminder_task
                    reminder.start_on = reminder_start_on
                    reminder.frequency = reminder_frequency
                    reminder.ramp_up = reminder_ramp_up
                    reminder.completed = False
                    reminder.save()
        else:
            Reminder.objects.filter(diary_entry=entry).delete()

        return redirect('diary_entry_case', case_id=entry.case.id)

    reminder = entry.reminders.first()
    return render(request, 'main/edit_business.html', {
        'entry': entry,
        'case': entry.case,
        'court_display': COURT_LABELS.get(entry.case.court, entry.case.court),
        'reminder': reminder,
        'today': datetime.date.today(),
    })


# ── SEARCH ──

@login_required
def case_search(request):
    query = request.GET.get('q', '').strip()
    court_level = request.GET.get('court_level', '')
    disposed_filter = request.GET.get('disposed', '')
    cases = search_cases(query=query, court_level=court_level, disposed=disposed_filter)

    case_list = list(cases)

    def sort_key(case):
        last_entry = case.diary_entries.order_by('-previous_date').first()
        if last_entry is None:
            return (0, -case.id)
        return (1, last_entry.previous_date.isoformat(), -case.id)

    case_list.sort(key=sort_key, reverse=True)

    for case in case_list:
        case.court_display_name = COURT_LABELS.get(case.court, case.court)
        last_entry = case.diary_entries.order_by('-next_date').first()
        if last_entry:
            case.next_date = last_entry.next_date
            case.prev_date = last_entry.previous_date
        else:
            case.next_date = None
            case.prev_date = None
    return render(request, 'main/search_cases.html', {
        'today': datetime.date.today(), 'cases': case_list, 'query': query, 'court_labels': COURT_LABELS,
        'court_level_choices': CourtLevel.choices,
        'selected_court_level': court_level, 'selected_disposed': disposed_filter,
    })


# ── CASE EXPORT ──

@login_required
def case_export_docx(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    entries = case.diary_entries.all()

    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    doc.add_heading(f'{case.case_type} {case.case_number}/{case.case_year}', 0)
    p = doc.add_paragraph()
    run1 = p.add_run(case.party_1)
    run1.bold = case.represents_party_1
    p.add_run(' vs ')
    run2 = p.add_run(case.party_2)
    run2.bold = case.represents_party_2
    doc.add_paragraph(f'Party Types: {case.party_1_type} / {case.party_2_type}   |   Representing: {case.representing}')
    doc.add_paragraph(f'Court: {COURT_LABELS.get(case.court, case.court)}, Hall: {case.court_hall}, Floor: {case.floor}')
    doc.add_paragraph(f'Jurisdiction: {case.get_jurisdiction_display()}   |   Level: {case.get_court_level_display()}')
    status = 'DISPOSED' if case.disposed else 'ACTIVE'
    doc.add_paragraph(f'Status: {status}')
    doc.add_paragraph('')

    if entries:
        doc.add_heading('Business History', level=1)
        for entry in entries:
            doc.add_heading(f'{entry.previous_date.strftime("%d %b %Y")}  →  {entry.next_date.strftime("%d %b %Y")}', level=2)
            doc.add_paragraph(f'Stage: {entry.stage}')
            doc.add_paragraph(entry.business)
            doc.add_paragraph(f'Entered by: {entry.advocate.get_full_name() or entry.advocate.username if entry.advocate else "—"} on {entry.created_at.strftime("%d %b %Y %I:%M %p")}')
            doc.add_paragraph('')
    else:
        doc.add_paragraph('No diary entries yet.')

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    filename = f'{case.case_type}_{case.case_number}_{case.case_year}.docx'.replace('/', '_')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    doc.save(response)
    return response


@login_required
def case_export_pdf(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    entries = case.diary_entries.all()

    from io import BytesIO
    from django.template.loader import render_to_string
    from weasyprint import HTML

    html = render_to_string('main/case_export_pdf.html', {
        'case': case, 'entries': entries, 'court_labels': COURT_LABELS,
        'court_display': COURT_LABELS.get(case.court, case.court),
    })
    pdf_file = BytesIO()
    HTML(string=html).write_pdf(pdf_file)
    pdf_file.seek(0)

    filename = f'{case.case_type}_{case.case_number}_{case.case_year}.pdf'.replace('/', '_')
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ── CAUSE LIST ──

@login_required
def cause_list(request):
    date_str = request.GET.get('date', '')
    entries = DiaryEntry.objects.none()
    errors = []

    if request.method == 'POST':
        date_str = request.POST.get('date', '')
        updated = 0
        entry_ids = set()
        for key in request.POST:
            if key.startswith('list_i_'):
                entry_ids.add(key.replace('list_i_', ''))
            elif key.startswith('list_ii_'):
                entry_ids.add(key.replace('list_ii_', ''))

        for eid in sorted(entry_ids):
            list_i_val = request.POST.get(f'list_i_{eid}', '').strip()
            list_ii_val = request.POST.get(f'list_ii_{eid}', '').strip()

            if list_ii_val and not list_i_val:
                errors.append(f'Case #{eid}: List II cannot be entered without List I.')
                continue

            try:
                case = Case.objects.get(id=eid)
            except Case.DoesNotExist:
                continue

            cl_entry, created = CauseListEntry.objects.get_or_create(
                date=datetime.datetime.strptime(date_str, '%Y-%m-%d').date(),
                case=case,
            )

            if list_i_val:
                try:
                    cl_entry.list_i = int(list_i_val)
                    cl_entry.list_ii = int(list_ii_val) if list_ii_val else 0
                    cl_entry.save()
                    updated += 1
                except ValueError:
                    errors.append(f'Case #{eid}: Invalid number.')
            else:
                if cl_entry.list_i is not None or cl_entry.list_ii is not None:
                    cl_entry.list_i = None
                    cl_entry.list_ii = None
                    cl_entry.save()
                    updated += 1

        if not errors and updated:
            messages.success(request, f'{updated} cause list number(s) updated.')
        return redirect(f'{request.path}?date={date_str}')

    if date_str:
        try:
            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            cl_entries = CauseListEntry.objects.filter(date=date_obj).select_related('case')
            cases_with_cl = {e.case_id for e in cl_entries}

            all_cases = Case.objects.filter(
                diary_entries__next_date=date_obj
            ).distinct()

            for case in all_cases:
                if case.id not in cases_with_cl:
                    CauseListEntry.objects.create(date=date_obj, case=case)

            entries = CauseListEntry.objects.filter(date=date_obj).select_related('case')

            court_order = [
                'city_civil', 'small_causes', 'city_civil_rural', 'dist_session_rural',
                'prl_senior_rural', 'prl_junior_rural',
                'cmm', 'mmtc', 'mmtc_mayo', 'cjm_cmm_rural', 'vacation_court_rural',
                'family',
                'vacation_bench_family',
                'consumer', 'urban_consumer',
                'commercial', 'commercial_court_rural',
                'mayo_hall',
                'labour_court_rural', 'labour_court_urban', 'senior_anekal', 'junior_anekal', 'hosakote',
                'devanahalli', 'doddaballapur', 'nelamangala', 'kr_puram',
                'high_court_karnataka', 'supreme_court_india',
            ]

            entries = sorted(entries, key=lambda e: (
                BUILDING_ORDER.index(COURT_TO_BUILDING.get(e.case.court, 'other'))
                    if e.case.court in COURT_TO_BUILDING else 999,
                court_order.index(e.case.court) if e.case.court in court_order else 999,
                (e.list_i or 0) + (e.list_ii or 0),
            ))

            diary_entries_for_stage = DiaryEntry.objects.filter(
                next_date=date_obj
            ).values('case_id', 'stage')
            stage_by_case = {de['case_id']: de['stage'] for de in diary_entries_for_stage}
            for sl, e in enumerate(entries, 1):
                e.sl_no = sl
                e.stage = stage_by_case.get(e.case.id, '')

        except ValueError:
            pass

    court_halls_on_date = set()
    if date_str:
        for e in entries:
            court_halls_on_date.add((e.case.court, e.case.court_hall))
    court_hall_notes = dict()
    for n in CourtHallNote.objects.all():
        key = f"{n.court}__{n.court_hall}"
        court_hall_notes[key] = n.note

    unique_hall_keys = []
    seen = set()
    for e in entries:
        key = f"{e.case.court}__{e.case.court_hall}"
        if key in court_hall_notes and key not in seen:
            seen.add(key)
            unique_hall_keys.append((e.case.court, e.case.court_hall))

    return render(request, 'main/cause_list.html', {
        'entries': entries, 'date_str': date_str, 'date_obj': date_obj if date_str else None,
        'court_labels': COURT_LABELS, 'errors': errors,
        'court_hall_notes': court_hall_notes,
        'unique_hall_keys': unique_hall_keys,
    })


@login_required
def cause_list_docx(request):
    date_str = request.GET.get('date', '')
    if not date_str:
        return HttpResponse('No date provided.', status=400)

    try:
        date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return HttpResponse('Invalid date.', status=400)

    from docx import Document
    from docx.shared import Pt, Inches, Cm
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    entries = CauseListEntry.objects.filter(date=date_obj).select_related('case')

    court_order = [
        'city_civil', 'small_causes', 'city_civil_rural', 'dist_session_rural',
        'prl_senior_rural', 'prl_junior_rural',
        'cmm', 'mmtc', 'mmtc_mayo', 'cjm_cmm_rural', 'vacation_court_rural',
        'family',
        'vacation_bench_family',
        'consumer', 'urban_consumer',
        'commercial', 'commercial_court_rural',
        'mayo_hall',
        'labour_court_rural', 'labour_court_urban', 'senior_anekal', 'junior_anekal', 'hosakote',
        'devanahalli', 'doddaballapur', 'nelamangala', 'kr_puram',
        'high_court_karnataka', 'supreme_court_india',
    ]
    entries = sorted(entries, key=lambda e: (
        BUILDING_ORDER.index(COURT_TO_BUILDING.get(e.case.court, 'other'))
            if e.case.court in COURT_TO_BUILDING else 999,
        court_order.index(e.case.court) if e.case.court in court_order else 999,
        (e.list_i or 0) + (e.list_ii or 0),
    ))

    diary_entries_for_stage = DiaryEntry.objects.filter(
        next_date=date_obj
    ).values('case_id', 'stage')
    stage_by_case = {de['case_id']: de['stage'] for de in diary_entries_for_stage}
    for sl, e in enumerate(entries, 1):
        e.sl_no = sl
        e.stage = stage_by_case.get(e.case.id, '')

    doc = Document()

    section = doc.sections[0]
    section.page_height = Cm(29.7)
    section.page_width = Cm(21.0)
    section.left_margin = Cm(1.5)
    section.right_margin = Cm(1.5)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    doc.add_heading(f'Cause List — {date_obj.strftime("%d %B %Y")}', 0)

    current_building = None

    for entry in entries:
        bldg_code = COURT_TO_BUILDING.get(entry.case.court, '')
        bldg_name = BUILDING_LABELS.get(bldg_code, COURT_LABELS.get(entry.case.court, entry.case.court))
        if bldg_name != current_building:
            current_building = bldg_name
            doc.add_heading(bldg_name, level=2)
            table = doc.add_table(rows=1, cols=6)
            table.style = 'Table Grid'
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            hdr = table.rows[0].cells
            headers = ['Sl No.', 'Floor', 'Case & Parties', 'Representing', 'Stage', 'Cause List']
            for i, h in enumerate(headers):
                hdr[i].text = h
                for p in hdr[i].paragraphs:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in p.runs:
                        run.bold = True
                        run.font.size = Pt(9)

        row = table.add_row().cells
        case_num = f"{entry.case.case_type}/{entry.case.case_number}/{entry.case.case_year}"
        parties = f"{entry.case.party_1} vs {entry.case.party_2}"
        cause_list_nos = f"List I: {entry.list_i or '—'}\nList II: {entry.list_ii or '—'}"
        data = [str(entry.sl_no), str(entry.case.floor), None, entry.case.representing, entry.stage or '—', cause_list_nos]
        for i, val in enumerate(data):
            if val is None:
                cell = row[i]
                p = cell.paragraphs[0]
                p.clear()
                run = p.add_run(f"{entry.case.court_hall}\n{case_num}\n")
                run.font.size = Pt(9)
                run1 = p.add_run(entry.case.party_1)
                run1.bold = entry.case.represents_party_1
                run1.font.size = Pt(9)
                run_vs = p.add_run(' vs ')
                run_vs.font.size = Pt(9)
                run2 = p.add_run(entry.case.party_2)
                run2.bold = entry.case.represents_party_2
                run2.font.size = Pt(9)
            else:
                row[i].text = val
                for p in row[i].paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(9)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    response['Content-Disposition'] = f'attachment; filename="cause_list_{date_str}.docx"'
    doc.save(response)
    return response


@login_required
def cause_list_pdf(request):
    date_str = request.GET.get('date', '')
    if not date_str:
        return HttpResponse('No date provided.', status=400)

    try:
        date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return HttpResponse('Invalid date.', status=400)

    from weasyprint import HTML

    entries = CauseListEntry.objects.filter(date=date_obj).select_related('case')

    court_order = [
        'city_civil', 'small_causes', 'city_civil_rural', 'dist_session_rural',
        'prl_senior_rural', 'prl_junior_rural',
        'cmm', 'mmtc', 'mmtc_mayo', 'cjm_cmm_rural', 'vacation_court_rural',
        'family',
        'vacation_bench_family',
        'consumer', 'urban_consumer',
        'commercial', 'commercial_court_rural',
        'mayo_hall',
        'labour_court_rural', 'labour_court_urban', 'senior_anekal', 'junior_anekal', 'hosakote',
        'devanahalli', 'doddaballapur', 'nelamangala', 'kr_puram',
        'high_court_karnataka', 'supreme_court_india',
    ]
    entries = sorted(entries, key=lambda e: (
        BUILDING_ORDER.index(COURT_TO_BUILDING.get(e.case.court, 'other'))
            if e.case.court in COURT_TO_BUILDING else 999,
        court_order.index(e.case.court) if e.case.court in court_order else 999,
        (e.list_i or 0) + (e.list_ii or 0),
    ))

    diary_entries_for_stage = DiaryEntry.objects.filter(
        next_date=date_obj
    ).values('case_id', 'stage')
    stage_by_case = {de['case_id']: de['stage'] for de in diary_entries_for_stage}
    for sl, e in enumerate(entries, 1):
        e.sl_no = sl
        e.stage = stage_by_case.get(e.case.id, '')

    html_str = render(request, 'main/cause_list_pdf.html', {
        'entries': entries, 'date_str': date_str, 'date_obj': date_obj,
        'court_labels': COURT_LABELS, 'court_to_building': COURT_TO_BUILDING,
    }).content.decode()

    pdf_file = BytesIO()
    HTML(string=html_str).write_pdf(pdf_file)
    pdf_file.seek(0)

    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="cause_list_{date_str}.pdf"'
    return response


# ── BATCH BUSINESS ENTRY ──

@login_required
def batch_new_case(request):
    cases = Case.objects.all().order_by('-id')
    selected_case = None
    case_id = request.GET.get('case_id') or request.POST.get('case_id')
    if case_id:
        selected_case = get_object_or_404(Case, id=case_id)

    if request.method == 'POST':
        case = get_object_or_404(Case, id=request.POST.get('case_id'))
        entries_data = []
        i = 0
        while True:
            previous_date = request.POST.get(f'previous_date_{i}')
            if previous_date is None:
                break
            next_date = request.POST.get(f'next_date_{i}')
            court = request.POST.get(f'court_{i}', '').strip()
            court_hall = request.POST.get(f'court_hall_{i}', '').strip()
            floor = request.POST.get(f'floor_{i}', '').strip()
            case_number_display = request.POST.get(f'case_number_display_{i}', '').strip()
            representing = request.POST.get(f'representing_{i}', '').strip()
            stage = request.POST.get(f'stage_{i}', '').strip()
            business = request.POST.get(f'business_{i}', '').strip()

            if previous_date and next_date and business:
                entries_data.append({
                    'previous_date': previous_date,
                    'next_date': next_date,
                    'court': court or COURT_LABELS.get(case.court, case.court),
                    'court_hall': court_hall or case.court_hall,
                    'floor': int(floor) if floor else case.floor,
                    'case_number_display': case_number_display or f"{case.case_type}/{case.case_number}/{case.case_year}",
                    'representing': representing or case.representing,
                    'stage': stage,
                    'business': business,
                })
            i += 1

        entries_data.sort(key=lambda e: e['previous_date'])

        for ed in entries_data:
            create_diary_entry(
                case=case,
                previous_date=ed['previous_date'],
                next_date=ed['next_date'],
                court=ed['court'],
                court_hall=ed['court_hall'],
                floor=ed['floor'],
                case_number_display=ed['case_number_display'],
                representing=ed['representing'],
                stage=ed['stage'],
                business=ed['business'],
                advocate=request.user,
            )

        if entries_data:
            messages.success(request, f'{len(entries_data)} business entr(y/ies) created.')
            return redirect('diary_entry_case', case_id=case.id)

    default_case_display = ''
    court_display = ''
    diary_entries = []
    if selected_case:
        default_case_display = f"{selected_case.case_type}/{selected_case.case_number}/{selected_case.case_year} — {selected_case.party_1} vs {selected_case.party_2}"
        court_display = COURT_LABELS.get(selected_case.court, selected_case.court)
        diary_entries = selected_case.diary_entries.all()

    return render(request, 'main/batch_new_case.html', {
        'cases': cases,
        'selected_case': selected_case,
        'default_case_display': default_case_display,
        'court_display': court_display,
        'court_labels': COURT_LABELS,
        'party1_choices': Party1Type.choices,
        'party2_choices': Party2Type.choices,
        'jurisdiction_choices': Jurisdiction.choices,
        'court_level_choices': CourtLevel.choices,
        'diary_entries': diary_entries,
    })


# ── TELEGRAM WEBHOOK ──

@csrf_exempt
@require_http_methods(['POST'])
def telegram_webhook(request):
    try:
        update = json.loads(request.body)
        from main.telegram_handler import process_update
        process_update(update)
    except Exception as e:
        logger.exception(f'Telegram webhook error: {e}')

    return HttpResponse('ok')


# ── COURT HALL SUGGESTIONS ──

@login_required
def suggest_court_halls(request):
    q = request.GET.get('q', '').strip()
    halls = Case.objects.values_list('court_hall', flat=True).distinct()
    if len(q) >= 1:
        halls = halls.filter(court_hall__icontains=q)
    halls = list(halls.order_by('court_hall')[:20])
    return JsonResponse([{'label': h, 'value': h} for h in halls], safe=False)


# ── COURT HALL NOTES ──

@login_required
def court_hall_notes(request):
    court = request.GET.get('court', '')
    court_hall = request.GET.get('court_hall', '')
    notes = CourtHallNote.objects.all()
    if court:
        notes = notes.filter(court=court)
    if court_hall:
        notes = notes.filter(court_hall__icontains=court_hall)
    return render(request, 'main/court_hall_notes.html', {
        'notes': notes.order_by('-updated_at'),
        'court': court,
        'court_hall': court_hall,
        'court_labels': COURT_LABELS,
    })


@login_required
def add_court_hall_note(request):
    if request.method == 'POST':
        court_code = request.POST.get('court')
        court_hall = request.POST.get('court_hall')
        note = request.POST.get('note', '').strip()
        if court_code and court_hall:
            obj, created = CourtHallNote.objects.get_or_create(
                court=court_code,
                court_hall=court_hall,
                defaults={'note': note},
            )
            if not created:
                if obj.note:
                    obj.note += f'\n\n---\n\n{note}'
                else:
                    obj.note = note
                obj.save()
            messages.success(request, 'Court hall note saved.')
        else:
            messages.error(request, 'Court and Court Hall are required.')
        return redirect(request.POST.get('next', 'cause_list'))
    court = request.GET.get('court', '')
    court_hall = request.GET.get('court_hall', '')
    existing_note = None
    if court and court_hall:
        try:
            existing_note = CourtHallNote.objects.get(court=court, court_hall=court_hall)
        except CourtHallNote.DoesNotExist:
            pass
    return render(request, 'main/add_court_hall_note.html', {
        'court': court,
        'court_hall': court_hall,
        'next': request.GET.get('next', 'cause_list'),
        'court_labels': COURT_LABELS,
        'existing_note': existing_note,
    })


# ── REMINDERS ──

@login_required
def mark_reminder_done(request, reminder_id):
    reminder = get_object_or_404(Reminder, id=reminder_id)
    reminder.completed = not reminder.completed
    reminder.save()
    return redirect('diary_entry_case', case_id=reminder.diary_entry.case.id)


@login_required
@user_passes_test(lambda u: u.is_superuser or u.userprofile.role == 'admin')
def send_reminders_now(request):
    from main.management.commands.send_reminders import send_due_reminders
    sent = send_due_reminders(auto=False)
    messages.success(request, f'{sent} reminder(s) sent to the Telegram group.')
    return redirect(request.META.get('HTTP_REFERER', 'home'))


# ── HOME ──

@login_required
def home(request):
    return render(request, 'index.html')
