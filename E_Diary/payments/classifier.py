import logging
import json
import re
from decimal import Decimal

from .services import _nvidia_chat, NVIDIA_CLASSIFIER_MODELS, is_cc_criminal
from .models import ChargeType
from main.models import DiaryEntry

logger = logging.getLogger(__name__)


def _repair_json(raw: str) -> str:
    raw = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip())
    raw = re.sub(r'\n?```\s*$', '', raw)
    result = []
    in_string = False
    escape = False
    for ch in raw:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            escape = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch in '\n\r':
            result.append('\\n')
            continue
        result.append(ch)
    return ''.join(result)


def _get_sides(case):
    rep = case.representing
    p1 = case.party_1_type
    p2 = case.party_2_type
    if rep == p1:
        return rep, p2
    elif rep == p2:
        return rep, p1
    return rep, 'opposite party'


def _is_accused_side(case):
    rep_lower = case.representing.lower()
    return 'accused' in rep_lower or 'respondent' in rep_lower


def _get_previous_entries(entry, limit=5):
    prevs = DiaryEntry.objects.filter(
        case=entry.case, entry_type='business',
        previous_date__lt=entry.previous_date
    ).order_by('-previous_date')[:limit]
    if not prevs:
        return "No previous entries available."
    lines = []
    for e in reversed(prevs):
        biz = (e.business or '')[:150]
        lines.append(f"[{e.previous_date}] Stage: {e.stage} — {biz}")
    return "\n".join(lines)


def _build_legal_rules(side, opposite, cc_criminal, stage, is_accused):
    if cc_criminal:
        ep_rule = (
            "filing_ep: ONLY if WE filed EP for OUR client. "
            "Under CrPC s.317, EP (Exemption Petition) is filed by the accused "
            "to seek exemption from personal appearance. "
        )
        if not is_accused:
            ep_rule += (
                f"We represent the {side} (not the accused). "
                "If the text says 'Accused filed EP' or 'they filed EP', "
                "that is the opposite party's work — DO NOT include filing_ep."
            )
        else:
            ep_rule += (
                f"We represent the {side} (the accused). "
                "If we filed EP for our client, include filing_ep."
            )
        procedure = "CrPC 1973 (Criminal Procedure Code)"
        extra_rules = (
            "- In criminal cases under CrPC, the accused can file EP for exemption.\n"
            "- The complainant/petitioner does NOT file EP.\n"
            "- IA under CrPC can be filed by either party for various reliefs.\n"
        )
    else:
        ep_rule = "filing_ep: Only applicable to CC criminal cases. Skip."
        procedure = "CPC 1908 (Civil Procedure Code)"
        extra_rules = (
            "- In civil cases under CPC, either party can file IAs.\n"
            "- CPC Order IX deals with ex-parte proceedings.\n"
            "- CPC Order XXXIX deals with interim injunctions.\n"
        )

    return procedure, ep_rule, extra_rules


def classify_business_entry(entry):
    case = entry.case
    advocate_text = (entry.business or '').strip()
    ecourts_text = (entry.ecourts_business or '').strip()
    stage = (entry.stage or '').strip()

    combined = f"Stage: {stage}\nAdvocate Notes: {advocate_text}\n"
    if ecourts_text:
        combined += f"eCourts: {ecourts_text}"

    if not advocate_text and not ecourts_text:
        return []

    cc_criminal = is_cc_criminal(case)
    side, opposite = _get_sides(case)
    is_accused = _is_accused_side(case)
    procedure, ep_rule, extra_rules = _build_legal_rules(side, opposite, cc_criminal, stage, is_accused)
    prev_context = _get_previous_entries(entry, 5)
    charge_types_all = ChargeType.objects.all()
    charge_list = "\n".join([f"- {ct.code}: {ct.name}" for ct in charge_types_all])

    system = (
        "You are a legal billing classifier for SHAILAJA LAW ASSOCIATES, an Indian law firm.\n\n"
        "IDENTITY: We represent the **" + side + "** in this case. "
        "The opposite party is the **" + opposite + "**.\n"
        "Procedure: " + procedure + "\n\n"
        "CORE RULE: Only bill for work OUR FIRM performed. "
        "If the opposite party did the work, DO NOT bill it.\n\n"
        "LEGAL RULES:\n"
        "- hearing: Include if our advocate appeared in court (any reason). "
        "'Parties appeared' means we appeared too — include.\n"
        "- evidence_chief: Include only if WE conducted examination-in-chief of OUR witness.\n"
        "- evidence_cross: Include only if WE cross-examined the opposite party's witness.\n"
        "- arguments: Include if WE argued before the court.\n"
        "- " + ep_rule + "\n"
        "- ia: Include if WE filed an IA.\n"
        "- ia_objections: Include if WE filed objections to an IA.\n"
        "- ia_hearing: Include if WE appeared for IA hearing.\n"
        "- preparation: Include if WE prepared documents (when we represent "
        "the petitioner/plaintiff side).\n"
        "- filing: Include if WE filed documents (petitioner side).\n"
        "- filing_vakalat: Include if WE filed vakalatnama (respondent side).\n"
        "- filing_objections: Include if WE filed objections (respondent side).\n"
        "- mediation: Include if mediation/hearing at mediation centre.\n\n"
        + extra_rules +
        "SUBJECT-VERB ANALYSIS (MOST IMPORTANT):\n"
        "Identify who performed EACH action in the business text:\n"
        "- If \"Accused filed EP\" → subject = Accused → THEY did it → NOT us.\n"
        "- If \"We filed EP\" → subject = Our firm → WE did it → billable.\n"
        "- If \"Advocate filed\" → subject = Advocate (us) → billable.\n"
        "- If \"they filed objections\" → 'they' = opposite party → NOT us.\n"
        "- If \"Parties appeared\" → both sides → WE appeared → bill hearing.\n"
        "- If passive voice like \"EP was filed\" → check stage & context "
        "to determine who.\n"
        "- Ambiguous? Look at the stage and case type for clues.\n\n"
        "PREVIOUS ENTRIES for context:\n" + prev_context + "\n\n"
        "AVAILABLE CHARGES:\n" + charge_list + "\n\n"
        "Return ONLY a JSON object: {\"charge_codes\": [\"code1\", \"code2\"], "
        "\"reasoning\": \"brief reasoning\"}\n"
        "Over-classifying (including uncertain charges) is better than "
        "under-classifying — leave it to human review."
    )

    user_msg = (
        f"Case: {case.case_type} {case.case_number}/{case.case_year}\n"
        f"Court: {case.court} ({case.court_hall})\n"
        f"Representing: {side}\n"
        f"Opposite: {opposite}\n\nEntry details:\n{combined}"
    )

    for model in NVIDIA_CLASSIFIER_MODELS:
        result = _nvidia_chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], temperature=0)
        if result is None:
            continue
        try:
            parsed = json.loads(_repair_json(result))
            codes = parsed.get('charge_codes', [])
            logger.info(f"Classifier ({model}) → {codes}")
            return codes
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Classifier ({model}) parse error: {e}, raw: {result[:200]}")
            continue

    logger.warning("All classifier models failed, returning empty list")
    return []
