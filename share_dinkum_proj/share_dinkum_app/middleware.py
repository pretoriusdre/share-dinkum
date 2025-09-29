
from django.contrib.auth import get_user_model, login

class AutoLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            user = get_user_model().objects.filter(is_superuser=True).first()
            if user:
                login(request, user)
        return self.get_response(request)