
from django.contrib.auth import get_user_model, login

class AutoLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.user = get_user_model().objects.filter(is_superuser=True).first()

    def __call__(self, request):
        if not request.user.is_authenticated and self.user:
            login(request, self.user)
        return self.get_response(request)