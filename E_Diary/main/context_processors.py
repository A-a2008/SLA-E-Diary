from .models import SiteSetting


def ecourts_settings(request):
    ctx = {
        'ecourts_toggle_on': SiteSetting.get_bool('ecourts_update_open', False),
    }
    if hasattr(request, 'user') and request.user.is_authenticated:
        profile = getattr(request.user, 'userprofile', None)
        if profile:
            ctx['user_can_ecourts'] = (
                request.user.is_superuser or
                profile.role == 'admin' or
                profile.can_access_ecourts
            )
        else:
            ctx['user_can_ecourts'] = request.user.is_superuser
    else:
        ctx['user_can_ecourts'] = False
    return ctx
