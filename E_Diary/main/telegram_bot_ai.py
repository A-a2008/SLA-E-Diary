import os
import logging
import datetime
from typing import Optional

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
LLM_MODEL = 'qwen/qwen3-32b'


class ClassificationResult(BaseModel):
    is_diary_entry: bool = Field(description="Whether the message is a diary entry update about a court case")
    reason: str = Field(description="Brief reason for the classification")


class MessageClassification(BaseModel):
    message_type: str = Field(description="Type of message: 'diary_entry' (court appearance update), 'cnr' (CNR number / eCourts reference for integration), 'case_update' (eCourts case status update/notice), or 'unrelated' (greeting, question, other)")
    reason: str = Field(description="Brief reason for the classification")
    cnr: Optional[str] = Field(description="If message_type is 'cnr', extract the 16-character CNR number here. Otherwise leave empty.")
    case_number: Optional[str] = Field(description="Case number if mentioned alongside CNR, to help match to our records")


class DiaryEntryExtraction(BaseModel):
    case_type: Optional[str] = Field(description="Case type abbreviation (e.g. CMC, OS, CrlP)")
    case_number: Optional[str] = Field(description="Case number")
    case_year: Optional[int] = Field(description="Case year (4 digits, e.g. 2025)")
    party_1: Optional[str] = Field(description="First party name")
    party_2: Optional[str] = Field(description="Second party name")
    previous_date: Optional[str] = Field(description="Date of court appearance (DD-MM-YYYY). If the user says 'today' or doesn't mention a previous date, leave this blank.")
    next_date: Optional[str] = Field(description="Next hearing date (DD-MM-YYYY). This MUST be present. For advance messages, this is the NEW date the case is being advanced to.")
    mediation_next_date: Optional[str] = Field(description="If the message mentions a SEPARATE mediation date that is different from the court next_date, extract it here in DD-MM-YYYY format. For example: 'mediation date 6/7, before court 7/9' → next_date='09-07-2026', mediation_next_date='06-07-2026'. Only set this if a distinct mediation date is explicitly mentioned alongside a court next_date.")
    business: str = Field(description="What happened in court today — the proceedings/status/order description. Keep it concise but informative.")
    stage: Optional[str] = Field(description="Stage of the case if mentioned (e.g. 'Arguments', 'Evidence', 'Judgment', 'Meditation')")
    mentions_reminder: Optional[bool] = Field(description="Whether the user mentioned anything about reminders at all (true/false/null if unclear)")
    wants_reminder: Optional[bool] = Field(description="If a reminder is mentioned, does the user want one? true/false. If not mentioned, leave null.")
    is_mediation: bool = Field(description="True ONLY if this entry is about an actual mediation/settlement conference session AT A MEDIATION CENTRE. False for a regular court hearing where mediation was merely mentioned or discussed (e.g. 'mediation report not received, next date given').")
    mediation_clarification_needed: Optional[bool] = Field(description="Set to True if the user mentions 'mediation' but you CANNOT tell whether they attended a regular court hearing (where mediation was discussed) OR had an actual mediation session at a mediation centre. The system will then ask the user to clarify. If you are confident, leave this null.")
    is_advance: bool = Field(description="True if the user is advancing/changing/preponing/postponing the next hearing date of an EXISTING case (e.g. 'advanced to 06/07', 'preponed to', 'next date changed to', 'advance application filed, next date 06/07'). The next_date field should contain the NEW date. When is_advance is True, no new diary entry is created — instead the last entry's next_date (and business/stage if mentioned) is updated. False for a normal new diary entry.")


class ReminderDetails(BaseModel):
    task: str = Field(description="What the reminder is about, e.g. 'Prepare arguments', 'File document'")
    frequency: str = Field(description="How often: daily, alternate, twice_week, or weekly")
    ramp_up: bool = Field(description="Whether to increase frequency as the next hearing date approaches")
    start_on: Optional[str] = Field(description="Start date in DD-MM-YYYY. If not specified, use today's date.")


def _get_llm():
    return ChatGroq(
        api_key=GROQ_API_KEY,
        model=LLM_MODEL,
        temperature=0,
        max_retries=2,
    )


def classify_message(text: str) -> ClassificationResult:
    llm = _get_llm().with_structured_output(MessageClassification)
    prompt = ChatPromptTemplate.from_messages([
        ('system', 'You are a legal assistant for an Indian law firm. Classify the user\'s message into one of these types:\n\n1. "diary_entry" — A court appearance update with case details (type/number/year), what happened in court, and next date.\n2. "cnr" — A CNR (Case Number Reference) — a 16-character alphanumeric code used for eCourts integration. This may be forwarded from the group or typed manually, possibly alongside a case number.\n3. "case_update" — An automatic eCourts case status update / notice (forwarded from the group).\n4. "unrelated" — Casual chat, greetings, questions not about a specific case.\n\nIf the message contains a 16-character CNR code, classify as "cnr" and extract the CNR.'),
        ('human', '{text}'),
    ])
    chain = prompt | llm
    return chain.invoke({'text': text})


def extract_diary_entry(text: str) -> DiaryEntryExtraction:
    llm = _get_llm().with_structured_output(DiaryEntryExtraction)
    prompt = ChatPromptTemplate.from_messages([
        ('system', '''You extract structured diary entry data from lawyer messages about Indian court cases. Today's date is ''' + datetime.date.today().strftime('%d-%m-%Y') + '''.

Rules:
- previous_date: If the user says "today" or doesn't mention a specific appearance date, leave it blank (null).
  If they mention a specific date they appeared, extract it in DD-MM-YYYY format.
- next_date: The next hearing date MUST be extracted in DD-MM-YYYY format. It's usually mentioned as "next date" or "next hearing".
  CRITICAL — If the user gives a date without a year (e.g. "31/7", "next date 15-07"), use the current year (2026).
  Only use a different year if the user explicitly mentions it (e.g. "15-07-2025").
- business: Reformatted version of what happened in court. Keep ALL details from the original message — do not remove, summarize, or redact anything. Fix spelling, capitalization, and grammar only. Preserve case numbers, dates, party names, order details, and any other specifics exactly as mentioned.
 - case_type: The case TYPE abbreviation (e.g. CC, OS, CMC, CrlP, WP). NOT the court name. Ignore court names like '52nd ACJM', 'CMM', 'City Civil', etc.
 - case_number: Just the numeric case number. If the user writes 'cc/6759/23', extract case_type='CC', case_number='6759', case_year=2023.
 - stage: Generate a SHORT, informative stage label (1-5 words). This will appear in the cause list. Be specific — mention the witness or document if relevant. Examples: 'Cross of DW1', 'Chief of PW2', 'Arguments', 'Hg', 'Evidence', 'Judgment', 'Order', 'Adjourned', 'Defense Evidence', 'Accused Statement', 'Further Chief', 'Final Arguments', 'Mediation'. NEVER leave this blank — infer from context.
 - mentions_reminder: Did the user say anything about reminders?
 - wants_reminder: Only set true/false if the user explicitly says they want or don't want a reminder.
 - mediation_next_date: ONLY set this when the message explicitly mentions a SEPARATE mediation date that is DIFFERENT from the court next_date. When the user says something like "mediation date 6/7, before court 7/9" or "mediation date 6/7. next date before court 7/9", set next_date='09-07-2026' (the court date) and mediation_next_date='06-07-2026' (the mediation date). If only a mediation date is mentioned with no separate court date, do NOT set this — just set next_date to the mediation date and is_mediation=True.

ADVANCE DETECTION (IMPORTANT):
- is_advance = True when the user is advancing/changing/preponing/postponing an existing case's next hearing date.
  Examples: "CC 6759/23 advanced to 06/07", "Advance application filed, next date 06/07/2026",
  "OS 1719/26 preponed to 01/07", "Next date changed to 15-07-2026 for CMC 123/25".
- When is_advance = True, the next_date field should contain the NEW date the case is being advanced to.
- When is_advance = True, the previous_date should contain the OLD next date if mentioned, otherwise leave blank.
- When is_advance = True, business should describe the reason/purpose of the advance (e.g. 'Advance application allowed', 'Advance filed by counsel', 'Matter preponed').
- is_advance = False for normal diary entries where the user actually appeared in court on a specific date.

CRITICAL — Mediation distinction:
- is_mediation = True ONLY if the user attended an ACTUAL MEDIATION SESSION at a mediation centre (e.g. "went to mediation centre", "had session with mediator", "mediation held", "parties negotiated at mediation").
- is_mediation = False if this is a REGULAR COURT HEARING where mediation was merely mentioned (e.g. "mediation report not received", "awaiting mediation report", "court said mediation pending", "next date for mediation report"). These are normal court appearances even though mediation is discussed.
- If the user's message mentions "mediation" but you genuinely cannot tell whether it was a court appearance or an actual mediation session, set mediation_clarification_needed = True and is_mediation = False.
- mediation_clarification_needed should be null if you are confident in your classification.

Examples:
- "Went to CCH-4, mediation report not received, next date 15-07-2026" → is_mediation=False, is_advance=False, mediation_clarification_needed=null
- "Attended mediation centre, session with mediator Mr. Kumar, settlement talks ongoing, next mediation 22-07" → is_mediation=True, is_advance=False, mediation_clarification_needed=null
- "The case went for mediation, next date is 15-07" → mediation_clarification_needed=True, is_mediation=False, is_advance=False (unclear if court or mediation centre)
- "CC 6759/23 advanced to 06-07-2026" → is_advance=True, next_date='06-07-2026', business='Advance application filed', is_mediation=False
- "Advance application filed for OS 1719/26, next date 01-07-2026, evidence of PW2" → is_advance=True, next_date='01-07-2026', stage='Evidence of PW2', business='Advance application allowed'
- "MC/2423/26, Vaishali vs. Venkatesh, filed vakalat on behalf of respondent. matter referred to mediation. mediation date 6/7. before court 7/9" → is_mediation=False, is_advance=False, next_date='09-07-2026', mediation_next_date='06-07-2026', business='Filed vakalat on behalf of respondent. Matter referred to mediation.', stage='Referred to Mediation'
- "OS 1234/25, parties attended mediation centre, mediation failed, next date 15-08-2026 for further proceedings" → is_mediation=True, is_advance=False, next_date='15-08-2026', mediation_next_date=null, business='Parties attended mediation centre. Mediation failed.', stage='Mediation Failed' '''),
        ('human', '{text}'),
    ])
    chain = prompt | llm
    return chain.invoke({'text': text})


def extract_reminder_details(text: str, next_date_str: str) -> ReminderDetails:
    today = datetime.date.today().strftime('%d-%m-%Y')
    llm = _get_llm().with_structured_output(ReminderDetails)
    prompt = ChatPromptTemplate.from_messages([
        ('system', f'''Extract reminder details from the user's message. Today is {today}. The next hearing date is {next_date_str}.

Frequency options: daily, alternate (alternate days), twice_week (Mondays and Thursdays), weekly.
Ramp_up: true means send daily reminders in the last week before the next hearing date.
start_on: Default to today ({today}) if not specified. Format DD-MM-YYYY.'''),
        ('human', '{text}'),
    ])
    chain = prompt | llm
    return chain.invoke({'text': text})


def handle_cnr_message(cnr: str, text: str) -> dict:
    """
    Handle a CNR message from Telegram.
    Tries to match the CNR to an existing case, or extracts the case number from the message.
    
    Returns: dict with keys 'cnr', 'case' (or None), 'case_number' (or None), 'message'
    """
    from main.models import Case

    cnr = (cnr or '').strip().upper()
    result = {'cnr': cnr, 'case': None, 'case_number': None, 'message': ''}

    if not cnr or len(cnr) != 16:
        result['message'] = 'Invalid CNR number (must be 16 characters).'
        return result

    # Try to match CNR to existing case
    case = Case.objects.filter(cnr=cnr).first()
    if case:
        result['case'] = case
        result['case_number'] = f"{case.case_type}/{case.case_number}/{case.case_year}"
        result['message'] = f'Found existing case: {result["case_number"]}'
        return result

    # Try to extract case number from the message text
    import re
    case_pattern = re.search(r'(\w{1,5})\s*[/]\s*(\d{1,6})\s*[/]\s*(\d{2,4})', text)
    if case_pattern:
        ct, cn, cy = case_pattern.group(1), case_pattern.group(2), case_pattern.group(3)
        if len(cy) == 2:
            cy = '20' + cy
        case = Case.objects.filter(
            case_type__iexact=ct,
            case_number=cn,
            case_year=cy,
        ).first()
        if case:
            case.cnr = cnr
            case.save()
            result['case'] = case
            result['case_number'] = f"{ct}/{cn}/{cy}"
            result['message'] = f'CNR saved to case {ct}/{cn}/{cy}.'
            return result

    result['message'] = f'CNR {cnr} received, but could not auto-match to a case. An admin can associate it via the website.'
    return result


def _clean_case_number(raw):
    import re
    raw = (raw or '').strip()
    # User often sends "cc/6759/23" — try splitting on / and find a purely numeric segment
    parts = raw.replace(',', '/').replace(' ', '/').split('/')
    for p in parts:
        p = p.strip()
        if p.isdigit() and len(p) <= 6:
            return p
    # Fallback: strip all non-digits
    return re.sub(r'[^0-9]', '', raw)


def match_case(extraction: DiaryEntryExtraction):
    from main.models import Case
    from django.db.models import Q

    raw_cn = extraction.case_number or ''
    clean_cn = _clean_case_number(raw_cn)

    candidates = Case.objects.all()

    # Strategy 1: exact match with all fields
    exact = Q()
    if extraction.case_type:
        exact &= Q(case_type__iexact=extraction.case_type)
    if clean_cn:
        exact &= Q(case_number=clean_cn)
    if extraction.case_year:
        exact &= Q(case_year=extraction.case_year)
    if extraction.party_1:
        exact &= Q(party_1__icontains=extraction.party_1)
    if extraction.party_2:
        exact &= Q(party_2__icontains=extraction.party_2)
    result = list(candidates.filter(exact))
    if len(result) == 1:
        return result[0]
    if len(result) > 1:
        return result[0]

    # Strategy 2: case_type + case_number + case_year (ignore parties)
    if extraction.case_type and clean_cn and extraction.case_year:
        result = list(candidates.filter(
            case_type__iexact=extraction.case_type,
            case_number=clean_cn,
            case_year=extraction.case_year,
        ))
        if len(result) == 1:
            return result[0]

    # Strategy 3: case_number + case_year + party_1 (ignore case_type)
    if clean_cn and extraction.case_year and extraction.party_1:
        f = Q(case_number=clean_cn, case_year=extraction.case_year)
        if extraction.party_2:
            f &= Q(party_2__icontains=extraction.party_2)
        result = list(candidates.filter(f))
        if len(result) == 1:
            return result[0]

    # Strategy 4: just case_number + party_1 (ignore year and type)
    if clean_cn and extraction.party_1:
        f = Q(case_number=clean_cn, party_1__icontains=extraction.party_1)
        if extraction.party_2:
            f &= Q(party_2__icontains=extraction.party_2)
        result = list(candidates.filter(f))
        if len(result) == 1:
            return result[0]

    # Strategy 5: case_number + party_1 (partial case_number match)
    if clean_cn and extraction.party_1:
        f = Q(case_number__icontains=clean_cn, party_1__icontains=extraction.party_1)
        if extraction.party_2:
            f &= Q(party_2__icontains=extraction.party_2)
        result = list(candidates.filter(f))
        if len(result) == 1:
            return result[0]

    return None


def parse_date(date_str):
    if not date_str:
        return None

    today = datetime.date.today()
    cleaned = date_str.strip().lower()

    if cleaned in ('today', 'now'):
        return today
    if cleaned in ('yesterday',):
        return today - datetime.timedelta(days=1)
    if cleaned in ('day before yesterday', '2 days ago'):
        return today - datetime.timedelta(days=2)
    if cleaned.startswith('last '):
        # "last monday", "last friday" etc.
        try:
            weekday_name = cleaned.split(' ', 1)[1][:3].title()
            weekday_map = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4, 'Sat': 5, 'Sun': 6}
            target = weekday_map.get(weekday_name)
            if target is not None:
                days_ahead = target - today.weekday()
                if days_ahead > 0:
                    days_ahead -= 7
                return today + datetime.timedelta(days=days_ahead)
        except (IndexError, KeyError):
            pass

    today = datetime.date.today()
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    # Try formats without year — infer from context (current year)
    for fmt in ('%d-%m', '%d/%m', '%d %m'):
        try:
            parsed = datetime.datetime.strptime(date_str.strip(), fmt).date()
            return parsed.replace(year=today.year)
        except ValueError:
            continue

    return None


def create_entry_from_extraction(extraction, case, advocate=None):
    from main.services import create_diary_entry
    from main.constants import COURT_LABELS
    from main.models import MediationStatus

    today = datetime.date.today()
    previous_date = parse_date(extraction.previous_date)
    if not previous_date:
        last_entry = case.diary_entries.order_by('-next_date').first()
        previous_date = last_entry.next_date if last_entry else today
    next_date = parse_date(extraction.next_date)
    if not next_date:
        logger.error(f'No valid next_date in extraction: {extraction.next_date}')
        return None

    # Sanity check: if next_date is more than 6 months in the past, assume AI got year wrong
    if next_date < today - datetime.timedelta(days=180):
        next_date = next_date.replace(year=today.year)
        logger.warning(f'Corrected next_date year to {next_date}')

    is_mediation = extraction.is_mediation and not extraction.mediation_clarification_needed
    clarification_needed = extraction.mediation_clarification_needed
    mediation_next_date = parse_date(extraction.mediation_next_date)

    # Check if mediation_next_date also needs year correction
    if mediation_next_date and mediation_next_date < today - datetime.timedelta(days=180):
        mediation_next_date = mediation_next_date.replace(year=today.year)

    # --- DUAL ENTRY: both a court next_date and a separate mediation date ---
    if mediation_next_date and not is_mediation:
        case.mediation_status = MediationStatus.REFERRED
        case.mediation_next_date = mediation_next_date
        case.save()

        # Create mediation diary entry
        create_diary_entry(
            case=case,
            entry_type='mediation',
            previous_date=previous_date,
            court='Karnataka Mediation Centre',
            court_hall='Mediation',
            floor=0,
            case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
            representing=case.representing,
            stage='Mediation',
            business=extraction.business.strip(),
            next_date=mediation_next_date,
            advocate=advocate,
            representing_parties=case.representing_parties,
            party_1_total=case.party_1_total,
            party_2_total=case.party_2_total,
        )

        # Create regular court diary entry
        court = COURT_LABELS.get(case.court, case.court)
        entry = create_diary_entry(
            case=case,
            entry_type='business',
            previous_date=previous_date,
            court=court,
            court_hall=case.court_hall,
            floor=case.floor,
            case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
            representing=case.representing,
            stage=(extraction.stage or '').strip(),
            business=extraction.business.strip(),
            next_date=next_date,
            advocate=advocate,
            representing_parties=case.representing_parties,
            party_1_total=case.party_1_total,
            party_2_total=case.party_2_total,
        )
        # Attach mediation info for the caller
        entry._mediation_entry_created = True
        entry._mediation_next_date = mediation_next_date
        return entry

    # --- SINGLE MEDIATION ENTRY ---
    if is_mediation:
        case.mediation_status = MediationStatus.REFERRED
        case.mediation_next_date = next_date
        case.save()
        court = 'Karnataka Mediation Centre'
        court_hall = 'Mediation'
        floor = 0
        entry_type = 'mediation'
    else:
        court = COURT_LABELS.get(case.court, case.court)
        court_hall = case.court_hall
        floor = case.floor
        entry_type = 'business'

        # If case is in mediation and this is a court entry, mark ongoing
        if case.mediation_status == MediationStatus.REFERRED:
            case.mediation_status = MediationStatus.ONGOING
            case.mediation_next_date = next_date
            case.save()
        elif case.mediation_status == MediationStatus.ONGOING:
            case.mediation_next_date = next_date
            case.save()

    entry = create_diary_entry(
        case=case,
        entry_type=entry_type,
        previous_date=previous_date,
        court=court,
        court_hall=court_hall,
        floor=floor,
        case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
        representing=case.representing,
        stage=(extraction.stage or '').strip(),
        business=extraction.business.strip(),
        next_date=next_date,
        advocate=advocate,
        representing_parties=case.representing_parties,
        party_1_total=case.party_1_total,
        party_2_total=case.party_2_total,
    )
    return entry


def update_last_entry_from_advance(extraction, case, advocate=None):
    from main.models import DiaryEntry, MediationStatus

    today = datetime.date.today()
    last_entry = case.diary_entries.order_by('-next_date').first()
    if not last_entry:
        return None

    next_date = parse_date(extraction.next_date)
    if not next_date:
        return None

    if next_date < today - datetime.timedelta(days=180):
        next_date = next_date.replace(year=today.year)

    last_entry.next_date = next_date
    if extraction.business and extraction.business.strip():
        last_entry.business = extraction.business.strip()
    if extraction.stage and extraction.stage.strip():
        last_entry.stage = extraction.stage.strip()
    if advocate:
        last_entry.advocate = advocate
    last_entry.save()

    if case.mediation_status in (MediationStatus.REFERRED, MediationStatus.ONGOING):
        case.mediation_next_date = next_date
        case.save()

    mediation_next_date = parse_date(extraction.mediation_next_date)
    if mediation_next_date and mediation_next_date < today - datetime.timedelta(days=180):
        mediation_next_date = mediation_next_date.replace(year=today.year)

    if mediation_next_date:
        # Also update the last mediation diary entry if one exists
        last_mediation = case.diary_entries.filter(entry_type='mediation').order_by('-next_date').first()
        if last_mediation:
            last_mediation.next_date = mediation_next_date
            last_mediation.save()
        case.mediation_status = MediationStatus.REFERRED
        case.mediation_next_date = mediation_next_date
        case.save()

    return last_entry
