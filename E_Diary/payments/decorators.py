from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect
from django.contrib import messages


def _payments_access(user):
    return user.is_superuser or user.groups.filter(name='payments').exists()


def payments_access_required(view_func):
    decorated = login_required(user_passes_test(_payments_access)(view_func))
    return decorated


def superuser_required(view_func):
    decorated = login_required(user_passes_test(lambda u: u.is_superuser)(view_func))
    return decorated
