from django.shortcuts import redirect
from django.contrib import messages


class PaymentsAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path.startswith('/payments/'):
            if not request.user.is_authenticated:
                return redirect('/login/?next=' + path)
            if not (request.user.is_superuser or request.user.groups.filter(name='payments').exists()):
                messages.error(request, 'You do not have access to the payments section.')
                return redirect('/')
        return self.get_response(request)
