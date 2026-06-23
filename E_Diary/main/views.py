from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from django.utils.crypto import get_random_string
from django.db.models import Q
from django.http import HttpResponse
from .models import Party1Type, Party2Type, Jurisdiction, CourtLevel, Case, DiaryEntry, UserProfile, UserRole
from .constants import COURT_LABELS
from .services import search_cases, get_latest_entry_data, create_diary_entry, create_case, dispose_case, reinstate_case
from io import BytesIO
import datetime



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

        create_case(
            jurisdiction=jurisdiction, court_level=court_level, court=court,
            court_hall=court_hall, floor=floor, case_type=case_type,
            case_number=case_number, case_year=case_year, party_1=party_1,
            party_1_type=party_1_type, party_2=party_2, party_2_type=party_2_type,
            representing=representing,
        )
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
    return render(request, 'main/diary_entry.html', {
        'cases': case_list, 'query': query, 'court_labels': COURT_LABELS,
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

    return render(request, 'main/diary_entry_case.html', {
        'case': case, 'entries': entries, 'court_labels': COURT_LABELS,
        'latest_data': latest_data, 'court_display': COURT_LABELS.get(case.court, case.court),
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
        representing = case.representing
        stage = request.POST.get('stage')
        business = request.POST.get('business')
        list_i = request.POST.get('list_i')
        list_ii = request.POST.get('list_ii')
        next_date = request.POST.get('next_date')

        create_diary_entry(
            case=case, previous_date=previous_date, court=court,
            court_hall=court_hall, floor=floor,
            case_number_display=case_number_display, representing=representing,
            stage=stage, business=business, next_date=next_date,
            advocate=request.user,
            list_i=int(list_i) if list_i else None,
            list_ii=int(list_ii) if list_ii else None,
        )
        return redirect('diary_entry_case', case_id=case.id)

    return render(request, 'main/add_business.html', {
        'case': case, 'latest_data': latest_data,
        'court_display': COURT_LABELS.get(case.court, case.court),
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
        entry.representing = entry.case.representing
        entry.stage = request.POST.get('stage')
        entry.business = request.POST.get('business')
        entry.list_i = int(request.POST.get('list_i')) if request.POST.get('list_i') else None
        entry.list_ii = int(request.POST.get('list_ii')) if request.POST.get('list_ii') else None
        entry.next_date = request.POST.get('next_date')
        entry.save()
        return redirect('diary_entry_case', case_id=entry.case.id)

    return render(request, 'main/edit_business.html', {
        'entry': entry,
        'case': entry.case,
        'court_display': COURT_LABELS.get(entry.case.court, entry.case.court),
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
    return render(request, 'main/search_cases.html', {
        'cases': case_list, 'query': query, 'court_labels': COURT_LABELS,
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
    p.add_run(f'{case.party_1} vs {case.party_2}').bold = True
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
            cl = ''
            if entry.list_i:
                cl += f'List I: {entry.list_i}'
            if entry.list_i and entry.list_ii:
                cl += ' | '
            if entry.list_ii:
                cl += f'List II: {entry.list_ii}'
            if cl:
                doc.add_paragraph(cl)
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
                errors.append(f'Entry #{eid}: List II cannot be entered without List I.')
                continue

            try:
                entry = DiaryEntry.objects.get(id=eid)
            except DiaryEntry.DoesNotExist:
                continue

            if list_i_val:
                try:
                    entry.list_i = int(list_i_val)
                    entry.list_ii = int(list_ii_val) if list_ii_val else 0
                    entry.save()
                    updated += 1
                except ValueError:
                    errors.append(f'Entry #{eid}: Invalid number.')
            else:
                if entry.list_i is not None or entry.list_ii is not None:
                    entry.list_i = None
                    entry.list_ii = None
                    entry.save()
                    updated += 1

        if not errors and updated:
            messages.success(request, f'{updated} cause list number(s) updated.')
        return redirect(f'{request.path}?date={date_str}')

    if date_str:
        try:
            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            entries = DiaryEntry.objects.filter(next_date=date_obj).select_related('case', 'advocate')

            court_order = [
                'cmm', 'mmtc', 'mmtc_mayo', 'city_civil', 'family', 'small_causes',
                'mayo_hall', 'commercial', 'consumer', 'urban_consumer',
                'city_civil_rural', 'dist_session_rural', 'prl_senior_rural',
                'prl_junior_rural', 'cjm_cmm_rural', 'vacation_court_rural',
                'commercial_court_rural', 'labour_court_rural', 'senior_anekal',
                'junior_anekal', 'hosakote', 'devanahalli', 'doddaballapur',
                'nelamangala', 'kr_puram',
                'high_court_karnataka', 'supreme_court_india',
            ]

            entries = sorted(entries, key=lambda e: (
                court_order.index(e.case.court) if e.case.court in court_order else 999,
                (e.list_i or 0) + (e.list_ii or 0),
            ))

        except ValueError:
            pass

    return render(request, 'main/cause_list.html', {
        'entries': entries, 'date_str': date_str, 'date_obj': date_obj if date_str else None,
        'court_labels': COURT_LABELS, 'errors': errors,
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

    entries = DiaryEntry.objects.filter(next_date=date_obj).select_related('case', 'advocate')

    court_order = [
        'cmm', 'mmtc', 'mmtc_mayo', 'city_civil', 'family', 'small_causes',
        'mayo_hall', 'commercial', 'consumer', 'urban_consumer',
        'city_civil_rural', 'dist_session_rural', 'prl_senior_rural',
        'prl_junior_rural', 'cjm_cmm_rural', 'vacation_court_rural',
        'commercial_court_rural', 'labour_court_rural', 'senior_anekal',
        'junior_anekal', 'hosakote', 'devanahalli', 'doddaballapur',
        'nelamangala', 'kr_puram', 'high_court_karnataka', 'supreme_court_india',
    ]
    entries = sorted(entries, key=lambda e: (
        court_order.index(e.case.court) if e.case.court in court_order else 999,
        (e.list_i or 0) + (e.list_ii or 0),
    ))

    doc = Document()
    doc.add_heading(f'Cause List — {date_obj.strftime("%d %B %Y")}', 0)

    current_court = None
    sl_no = 0

    for entry in entries:
        court_name = COURT_LABELS.get(entry.case.court, entry.case.court)
        if court_name != current_court:
            current_court = court_name
            doc.add_heading(court_name, level=2)
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
            sl_no = 0

        sl_no += 1
        row = table.add_row().cells
        case_num = f"{entry.case.case_type}/{entry.case.case_number}/{entry.case.case_year}"
        parties = f"{entry.case.party_1} vs {entry.case.party_2}"
        cause_list_nos = f"List I: {entry.list_i or '—'}\nList II: {entry.list_ii or '—'}"
        data = [str(sl_no), str(entry.floor), f"{entry.court_hall}\n{case_num}\n{parties}", entry.representing, entry.stage, cause_list_nos]
        for i, val in enumerate(data):
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

    entries = DiaryEntry.objects.filter(next_date=date_obj).select_related('case', 'advocate')

    court_order = [
        'cmm', 'mmtc', 'mmtc_mayo', 'city_civil', 'family', 'small_causes',
        'mayo_hall', 'commercial', 'consumer', 'urban_consumer',
        'city_civil_rural', 'dist_session_rural', 'prl_senior_rural',
        'prl_junior_rural', 'cjm_cmm_rural', 'vacation_court_rural',
        'commercial_court_rural', 'labour_court_rural', 'senior_anekal',
        'junior_anekal', 'hosakote', 'devanahalli', 'doddaballapur',
        'nelamangala', 'kr_puram', 'high_court_karnataka', 'supreme_court_india',
    ]
    entries = sorted(entries, key=lambda e: (
        court_order.index(e.case.court) if e.case.court in court_order else 999,
        (e.list_i or 0) + (e.list_ii or 0),
    ))

    html_str = render(request, 'main/cause_list_pdf.html', {
        'entries': entries, 'date_str': date_str, 'date_obj': date_obj,
        'court_labels': COURT_LABELS,
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
        created_count = 0
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
            list_i = request.POST.get(f'list_i_{i}', '').strip()
            list_ii = request.POST.get(f'list_ii_{i}', '').strip()

            if previous_date and next_date and business:
                create_diary_entry(
                    case=case,
                    previous_date=previous_date,
                    next_date=next_date,
                    court=court or COURT_LABELS.get(case.court, case.court),
                    court_hall=court_hall or case.court_hall,
                    floor=int(floor) if floor else case.floor,
                    case_number_display=case_number_display or f"{case.case_type}/{case.case_number}/{case.case_year}",
                    representing=representing or case.representing,
                    stage=stage,
                    business=business,
                    advocate=request.user,
                    list_i=int(list_i) if list_i else None,
                    list_ii=int(list_ii) if list_ii else None,
                )
                created_count += 1
            i += 1

        if created_count:
            messages.success(request, f'{created_count} business entr(y/ies) created.')
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


# ── HOME ──

@login_required
def home(request):
    return render(request, 'index.html')
