from django import template
from ..constants import COURT_LABELS, COURT_TO_BUILDING, BUILDING_LABELS

register = template.Library()


@register.filter
def court_label(court_code):
    return COURT_LABELS.get(court_code, court_code)


@register.filter
def building_code(court_code):
    return COURT_TO_BUILDING.get(court_code, '')


@register.filter
def building_label(building_code):
    return BUILDING_LABELS.get(building_code, building_code)


@register.filter
def dict_key(d, key):
    return d.get(key, '')


@register.filter
def dict_key_exists(d, key):
    return key in d
