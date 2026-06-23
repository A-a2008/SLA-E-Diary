from django import template
from ..constants import COURT_LABELS

register = template.Library()


@register.filter
def court_label(court_code):
    return COURT_LABELS.get(court_code, court_code)
