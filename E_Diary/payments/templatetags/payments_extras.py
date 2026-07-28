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
def client_balance(client):
    from payments.services import get_client_balance
    return get_client_balance(client)


@register.filter
def filter_charges(charge_types, entry_type):
    if entry_type == 'mediation':
        return [ct for ct in charge_types if ct.code == 'mediation_attended']
    return [ct for ct in charge_types if ct.code != 'mediation_attended']


@register.filter
def dict_key(d, key):
    if d is None:
        return ''
    val = d.get(key)
    if val is None:
        return ''
    if hasattr(val, 'amount'):
        if val.amount is None:
            return ''
        return str(val.amount)
    return val
