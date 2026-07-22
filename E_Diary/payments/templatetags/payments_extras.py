from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def format_amount(value):
    if value is None or float(value) == 0:
        return 'NIL'
    amount = float(value)
    if amount == int(amount):
        return f'\u20b9{int(amount):,}'
    return f'\u20b9{amount:,.2f}'


@register.filter
def total_amount(charge_items):
    total = sum(float(item.amount or 0) for item in charge_items.all())
    if total == int(total):
        return f'\u20b9{int(total):,}'
    return f'\u20b9{total:,.2f}'


@register.filter
def is_cc_criminal(case):
    from payments.services import is_cc_criminal as check_cc
    return check_cc(case)


@register.filter
def dict_key(d, key):
    if d is None:
        return ''
    val = d.get(key)
    if val is None:
        return ''
    if hasattr(val, 'amount') and val.amount is None:
        return ''
    if hasattr(val, 'amount'):
        return val.amount
    return val
