"""URL configuration for share_dinkum_proj."""
from django.contrib import admin
from django.urls import path


from django.conf import settings


from django.templatetags.static import static as static_url
from django.views.generic import RedirectView

from share_dinkum_app.portfolio_scope import serve_media

# The site header, title and index title are attributes of ShareDinkumAdminSite, which is
# installed as the default admin site by ShareDinkumAdminConfig.

urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=True)),  # Redirect root URL to admin
    # Browsers request /favicon.ico from the site root whether or not a page links to it, so without
    # this every page load logs a 404. The icon itself lives in the app's static directory.
    path('favicon.ico', RedirectView.as_view(url=static_url('favicon.ico'))),
    path('admin/', admin.site.urls),
    # Attached documents and exports, each only to a user who may see its portfolio.
    path(f'{settings.MEDIA_URL.strip("/")}/<path:path>', admin.site.admin_view(serve_media)),
]
