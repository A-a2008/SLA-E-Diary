"""
Service layer for SLA E-Diary.

All core business logic lives here, separate from HTTP views.
This allows reuse from:
  - Django views (main/views.py)
  - Telegram bot (future)
  - Management commands
  - API endpoints (future)
"""

from django.db.models import QuerySet
from django.contrib.auth.models import User
from .models import Case, DiaryEntry


def create_case(
    jurisdiction: str,
    court_level: str,
    court: str,
    court_hall: str,
    floor: int,
    case_type: str,
    case_number: str,
    case_year: int,
    party_1: str,
    party_1_type: str,
    party_2: str,
    party_2_type: str,
    representing: str,
    representing_parties: str = '1',
    party_1_total: int = 1,
    party_2_total: int = 1,
) -> Case:
    return Case.objects.create(
        jurisdiction=jurisdiction,
        court_level=court_level,
        court=court,
        court_hall=court_hall,
        floor=floor,
        case_type=case_type,
        case_number=case_number,
        case_year=case_year,
        party_1=party_1,
        party_1_type=party_1_type,
        party_2=party_2,
        party_2_type=party_2_type,
        representing=representing,
        representing_parties=representing_parties,
        party_1_total=party_1_total,
        party_2_total=party_2_total,
    )


def search_cases(
    query: str = "",
    court_level: str = "",
    disposed: str = "",
) -> QuerySet[Case]:
    from django.db.models import Q

    cases = Case.objects.all()

    if query:
        if '/' in query:
            parts = query.split('/')
            num = parts[0]
            year = parts[1]
            if len(year) == 2:
                year = "20" + year
            cases = cases.filter(
                Q(case_number__icontains=num, case_year__icontains=year) |
                Q(party_1__icontains=query) |
                Q(party_2__icontains=query) |
                Q(case_type__icontains=query)
            )
        else:
            cases = cases.filter(
                Q(case_number__icontains=query) |
                Q(case_year__icontains=query) |
                Q(party_1__icontains=query) |
                Q(party_2__icontains=query) |
                Q(case_type__icontains=query) |
                Q(court__icontains=query)
            )

    if court_level:
        cases = cases.filter(court_level=court_level)

    if disposed == 'active':
        cases = cases.filter(disposed=False)
    elif disposed == 'disposed':
        cases = cases.filter(disposed=True)

    return cases


def get_latest_entry_data(case: Case) -> dict:
    latest = case.diary_entries.order_by('-previous_date').first()
    if latest:
        return {
            'previous_date': latest.next_date,
            'court': latest.court,
            'court_hall': latest.court_hall,
            'floor': latest.floor,
            'case_number_display': latest.case_number_display,
            'representing': latest.representing,
            'representing_parties': latest.representing_parties,
            'party_1_total': latest.party_1_total,
            'party_2_total': latest.party_2_total,
            'stage': latest.stage,
        }
    from .constants import COURT_LABELS
    return {
        'previous_date': None,
        'court': COURT_LABELS.get(case.court, case.court),
        'court_hall': case.court_hall,
        'floor': case.floor,
        'case_number_display': f"{case.case_type}/{case.case_number}/{case.case_year}",
        'representing': case.representing,
        'representing_parties': case.representing_parties,
        'party_1_total': case.party_1_total,
        'party_2_total': case.party_2_total,
        'stage': '',
    }


def create_diary_entry(
    case: Case,
    previous_date,
    court: str,
    court_hall: str,
    floor: int,
    case_number_display: str,
    representing: str,
    stage: str,
    business: str,
    next_date,
    advocate: User = None,
    representing_parties: str = '1',
    party_1_total: int = 1,
    party_2_total: int = 1,
    entry_type: str = 'business',
) -> DiaryEntry:
    return DiaryEntry.objects.create(
        case=case,
        entry_type=entry_type,
        previous_date=previous_date,
        court=court,
        court_hall=court_hall,
        floor=floor,
        case_number_display=case_number_display,
        representing=representing,
        representing_parties=representing_parties,
        party_1_total=party_1_total,
        party_2_total=party_2_total,
        stage=stage,
        business=business,
        next_date=next_date,
        advocate=advocate,
    )


def dispose_case(case: Case) -> Case:
    case.disposed = True
    case.save()
    return case


def reinstate_case(case: Case) -> Case:
    case.disposed = False
    case.save()
    return case
