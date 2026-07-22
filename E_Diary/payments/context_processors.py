def payments_access(request):
    ctx = {'user_can_payments': False}
    if hasattr(request, 'user') and request.user.is_authenticated:
        ctx['user_can_payments'] = (
            request.user.is_superuser or
            request.user.groups.filter(name='payments').exists()
        )
    return ctx
