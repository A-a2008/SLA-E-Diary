from .models import SiteSetting


def ecourts_settings(request):
    return {
        'ecourts_toggle_on': SiteSetting.get_bool('ecourts_update_open', False),
    }
