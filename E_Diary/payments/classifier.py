import logging
import json

from .services import _nvidia_chat, NVIDIA_CLASSIFIER_MODELS, is_cc_criminal
from .models import ChargeType

logger = logging.getLogger(__name__)


def classify_business_entry(entry):
    case = entry.case
    advocate_text = (entry.business or '').strip()
    ecourts_text = (entry.ecourts_business or '').strip()
    stage = (entry.stage or '').strip()
    combined = f"Stage: {stage}\nAdvocate Notes: {advocate_text}\n"
    if ecourts_text:
        combined += f"eCourts: {ecourts_text}"
    if not combined.strip() or combined.strip() == "Stage: \nAdvocate Notes: \n":
        return []
    charge_types = ChargeType.objects.all()
    charge_list = "\n".join([f"- {ct.code}: {ct.name}" for ct in charge_types])
    representing = case.representing
    party_1 = case.party_1_type
    party_2 = case.party_2_type
    if representing == party_1:
        side = "petitioner"
    elif representing == party_2:
        side = "respondent"
    else:
        side = "both"
    cc_criminal = is_cc_criminal(case)
    system = (
        "You are a legal billing classifier for an Indian law firm. "
        "Given a court diary entry (stage + business description), determine which charge types apply.\n\n"
        "RULES:\n"
        "1. Return ONLY a JSON object with a \"charge_codes\" array of strings and a \"reasoning\" string.\n"
        "2. The charge_codes must ONLY include codes from the provided list.\n"
        "3. If the entry mentions hearing, appearance, or the advocate 'appeared' → include 'hearing'.\n"
        "4. If evidence chief examination is mentioned → include 'evidence_chief'.\n"
        "5. If cross examination is mentioned → include 'evidence_cross'.\n"
        "6. If arguments are mentioned → include 'arguments'.\n"
        "7. If mediation is mentioned → include 'mediation'.\n"
        "8. If IA is mentioned in the stage or business → include relevant IA charge(s).\n"
        "9. If filing/arguments on IA → include 'ia_hearing'.\n"
        "10. Only include 'filing_ep' if this is a CC criminal case.\n"
        "11. If the advocate filed or prepared something on the petitioner side → include 'preparation'/'filing'.\n"
        "12. If Vakalat was filed on the respondent side → include 'filing_vakalat'.\n"
        "13. If objections were filed on the respondent side → include 'filing_objections'.\n"
        "14. Be conservative but INCLUSIVE — if you're unsure, INCLUDE the charge. "
        "It's better to over-classify (for human review) than to miss a billable charge.\n\n"
        f"Available charge types:\n{charge_list}\n"
        f"Representing side: {side}\n"
        f"Is CC Criminal case (filing_ep applicable): {cc_criminal}"
    )
    user_msg = f"Case: {case.case_type} {case.case_number}/{case.case_year}\nCourt: {case.court}\n\nEntry details:\n{combined}"
    for model in NVIDIA_CLASSIFIER_MODELS:
        result = _nvidia_chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], temperature=0)
        if result is None:
            continue
        try:
            cleaned = result.strip()
            if cleaned.startswith('```'):
                cleaned = cleaned.split('\n', 1)[-1] if '\n' in cleaned else cleaned[3:]
                cleaned = cleaned.strip()
                if cleaned.endswith('```'):
                    cleaned = cleaned[:-3].strip()
            parsed = json.loads(cleaned)
            codes = parsed.get('charge_codes', [])
            logger.info(f"Classifier ({model}) → {codes}")
            return codes
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Classifier ({model}) parse error: {e}, raw: {result[:200]}")
            continue
    logger.warning("All classifier models failed, returning empty list")
    return []
