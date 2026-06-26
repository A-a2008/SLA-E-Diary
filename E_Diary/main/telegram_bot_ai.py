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


class DiaryEntryExtraction(BaseModel):
    case_type: Optional[str] = Field(description="Case type abbreviation (e.g. CMC, OS, CrlP)")
    case_number: Optional[str] = Field(description="Case number")
    case_year: Optional[int] = Field(description="Case year (4 digits, e.g. 2025)")
    party_1: Optional[str] = Field(description="First party name")
    party_2: Optional[str] = Field(description="Second party name")
    previous_date: Optional[str] = Field(description="Date of court appearance (DD-MM-YYYY). If the user says 'today' or doesn't mention a previous date, leave this blank.")
    next_date: Optional[str] = Field(description="Next hearing date (DD-MM-YYYY). This MUST be present.")
    business: str = Field(description="What happened in court today — the proceedings/status/order description. Keep it concise but informative.")
    stage: Optional[str] = Field(description="Stage of the case if mentioned (e.g. 'Arguments', 'Evidence', 'Judgment')")
    mentions_reminder: Optional[bool] = Field(description="Whether the user mentioned anything about reminders at all (true/false/null if unclear)")
    wants_reminder: Optional[bool] = Field(description="If a reminder is mentioned, does the user want one? true/false. If not mentioned, leave null.")


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
    llm = _get_llm().with_structured_output(ClassificationResult)
    prompt = ChatPromptTemplate.from_messages([
        ('system', 'You are a legal assistant for an Indian law firm. Classify whether the user\'s message is a diary entry update about a court case.\n\nA diary entry update includes: case type/number/year, what happened in court, and a next hearing date.\n\nNon-diary messages: casual chat, greetings, questions not about a specific case.'),
        ('human', '{text}'),
    ])
    chain = prompt | llm
    return chain.invoke({'text': text})


def extract_diary_entry(text: str) -> DiaryEntryExtraction:
    llm = _get_llm().with_structured_output(DiaryEntryExtraction)
    prompt = ChatPromptTemplate.from_messages([
        ('system', '''You extract structured diary entry data from lawyer messages about Indian court cases.

Rules:
- previous_date: If the user says "today" or doesn't mention a specific appearance date, leave it blank (null).
  If they mention a specific date they appeared, extract it in DD-MM-YYYY format.
- next_date: The next hearing date MUST be extracted. It's usually mentioned as "next date" or "next hearing".
- business: A concise description of what happened in court.
 - case_type: The case TYPE abbreviation (e.g. CC, OS, CMC, CrlP, WP). NOT the court name. Ignore court names like '52nd ACJM', 'CMM', 'City Civil', etc.
 - case_number: Just the numeric case number. If the user writes 'cc/6759/23', extract case_type='CC', case_number='6759', case_year=2023.
 - stage: Generate a SHORT, informative stage label (1-5 words). This will appear in the cause list. Be specific — mention the witness or document if relevant. Examples: 'Cross of DW1', 'Chief of PW2', 'Arguments', 'Hg', 'Evidence', 'Judgment', 'Order', 'Adjourned', 'Defense Evidence', 'Accused Statement', 'Further Chief', 'Final Arguments'. NEVER leave this blank — infer from context.
 - mentions_reminder: Did the user say anything about reminders?
 - wants_reminder: Only set true/false if the user explicitly says they want or don't want a reminder.'''),
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

    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def create_entry_from_extraction(extraction, case, advocate=None):
    from main.services import create_diary_entry
    from main.constants import COURT_LABELS

    today = datetime.date.today()
    previous_date = parse_date(extraction.previous_date) or today
    next_date = parse_date(extraction.next_date)
    if not next_date:
        logger.error(f'No valid next_date in extraction: {extraction.next_date}')
        return None

    court = COURT_LABELS.get(case.court, case.court)
    entry = create_diary_entry(
        case=case,
        previous_date=previous_date,
        court=court,
        court_hall=case.court_hall,
        floor=case.floor,
        case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
        representing=case.representing,
        stage=extraction.stage or '',
        business=extraction.business,
        next_date=next_date,
        advocate=advocate,
        representing_parties=case.representing_parties,
        party_1_total=case.party_1_total,
        party_2_total=case.party_2_total,
    )
    return entry
