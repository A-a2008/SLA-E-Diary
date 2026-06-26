from django import template
from ..constants import COURT_LABELS

register = template.Library()


@register.filter
def court_label(court_code):
    return COURT_LABELS.get(court_code, court_code)


@register.filter
def dict_key(d, key):
    return d.get(key, '')


@register.filter
def dict_key_exists(d, key):
    return key in d
