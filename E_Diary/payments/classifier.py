import logging
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

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
    prev_context = _get_previous_entries(entry, 5)
    charge_types_all = ChargeType.objects.all()
    charge_list = "\n".join([f"- {ct.code}: {ct.name}" for ct in charge_types_all])

    ep_rule = (
        "filing_ep: ONLY if WE are the accused/respondent AND we filed EP for our client. "
        "Under CrPC s.317, EP is filed by the accused to seek exemption from appearance. "
        + ("We represent the accused — check if we filed EP." if is_accused else
           "We do NOT represent the accused — accused filing EP is THEIR work, NOT ours.")
    )

    system = (
        "You are a legal billing classifier for SHAILAJA LAW ASSOCIATES, an Indian law firm.\n\n"
        "IDENTITY: We represent the **" + side + "**. "
        "Opposite party: **" + opposite + "**.\n\n"

        "CARDINAL RULES (strictly follow):\n\n"

        "1. HEARING/APPEARANCE: ALWAYS include this if we appeared in court "
        "that day. It is the DEFAULT charge for any court attendance. "
        "If we were present, bill hearing/appearance. This applies regardless "
        "of what else was done.\n\n"

        "2. PREPARATION: ONLY when the case was FIRST filed / initially instituted. "
        "NEVER include for subsequent steps like filing an IA, filing a memo, "
        "filing objections, etc. Preparation is a ONE-TIME charge at case commencement.\n\n"

        "3. FILING: ONLY when the case was FIRST filed / initially instituted. "
        "NEVER include for filing an IA, filing extension application, filing "
        "withdrawal memo, filing vakalat, etc. Filing is a ONE-TIME charge at "
        "case commencement.\n\n"

        "4. IA: Include ONLY if WE filed an Interlocutory Application. "
        "Examples: extension IA, stay IA, exemption IA. NOT for filing main case.\n\n"

        "5. IA OBJECTIONS: Include ONLY if WE filed objections to an IA "
        "filed by the OPPOSITE party. This is MUTUALLY EXCLUSIVE with IA — "
        "do NOT include both IA and IA Objections unless the entry clearly "
        "describes US filing BOTH an IA AND filing objections to their IA.\n\n"

        "6. IA HEARING: Include if WE appeared before the judge specifically "
        "for an IA hearing / made submissions on an IA on THIS DATE. "
        "If the text says 'for hearing on IA' as a future scheduled event "
        "(after 'next date', 'extended till', 'adjourned to'), "
        "it is NOT today's work — DO NOT include IA Hearing.\n\n"

        "7. " + ep_rule + "\n\n"

        "8. evidence_chief: Only if WE conducted examination-in-chief of OUR witness.\n"
        "9. evidence_cross: Only if WE cross-examined THEIR witness.\n"
        "10. arguments: Only if WE made final arguments/submissions.\n"
        "11. mediation: Only if mediation session occurred.\n"
        "12. filing_vakalat: Only if WE filed vakalatnama (respondent side).\n"
        "13. filing_objections: Only if WE filed objections (respondent side).\n\n"

        "TEMPORAL SCOPE (CRITICAL — MOST COMMON MISTAKE):\n"
        "Only bill for work that happened ON THIS DATE's proceedings. "
        "The business text often MIXES three timeframes:\n"
        "- TODAY's proceedings → BILLABLE\n"
        "- What is scheduled for NEXT DATE → NOT BILLABLE (future event)\n"
        "- What happened on PREVIOUS dates → NOT BILLABLE (past, context only)\n\n"
        "TEMPORAL INDICATORS:\n"
        "  FUTURE (DO NOT bill): 'for next date', 'adjourned to', "
        "'extended till', 'listed on', 'posted to', 'next date for', "
        "'for hearing on', 'for further chief', 'for arguments on'\n"
        "  TODAY (bill): 'present', 'files', 'argued', 'submitted', "
        "'appeared', 'prayed for', 'filed', 'allowed', 'dismissed'\n\n"
        "EXAMPLES:\n"
        "- 'Filed extension IA. Extended till next date, for hearing on IA 1'\n"
        "  → Today: present (hearing/appearance) + filed IA (IA)\n"
        "  → NOT today: 'for hearing on IA 1' is NEXT DATE → DO NOT include IA Hearing\n"
        "- 'Parties appeared. Arguments heard. Judgment reserved.'\n"
        "  → Today: present (hearing/appearance) + arguments (arguments)\n"
        "- 'Cross examination of PW1 done. Next date for further chief'\n"
        "  → Today: present (hearing/appearance) + cross done (evidence_cross)\n"
        "  → NOT today: 'for further chief' is NEXT DATE → DO NOT include evidence_chief\n\n"

        "SUBJECT-VERB ANALYSIS:\n"
        "- Who did the action? 'We filed' = our work. 'They filed' = NOT ours.\n"
        "- 'Filed extension IA' → implies WE filed it (advocate notation) → IA.\n"
        "- 'Accused filed EP' → opposite party → NOT our work.\n"
        "- 'Parties appeared' → WE appeared → bill hearing/appearance.\n\n"

        "PREVIOUS ENTRIES CONTEXT:\n" + prev_context + "\n\n"

        "AVAILABLE CHARGES:\n" + charge_list + "\n\n"

        "Return ONLY JSON: {\"charge_codes\": [\"code1\", ...], \"reasoning\": \"...\"}\n"
        "If unsure, INCLUDE rather than exclude (human review will correct)."
    )

    user_msg = (
        f"Case: {case.case_type} {case.case_number}/{case.case_year}\n"
        f"Court: {case.court} ({case.court_hall})\n"
        f"Representing: {side}\n"
        f"Opposite: {opposite}\n"
        f"Entry date: {entry.previous_date}\n\n"
        f"Entry details:\n{combined}"
    )

    def _call(model):
        return model, _nvidia_chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], temperature=0, timeout=25, rate_limit=False)

    top_models = NVIDIA_CLASSIFIER_MODELS[:3]
    with ThreadPoolExecutor(max_workers=len(top_models)) as pool:
        fut_map = {pool.submit(_call, m): m for m in top_models}
        try:
            for future in as_completed(fut_map, timeout=50):
                model, result = future.result()
                if result is None:
                    continue
                try:
                    parsed = json.loads(_repair_json(result))
                    codes = parsed.get('charge_codes', [])
                    logger.info(f"Classifier ({model}) → {codes}")
                    pool.shutdown(wait=False)
                    return codes
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Classifier ({model}) parse error: {e}")
                    continue
        except TimeoutError:
            logger.warning("Classifier timed out on all parallel models")
            pool.shutdown(wait=False)

    logger.warning("All classifier models failed, returning empty list")
    return []
