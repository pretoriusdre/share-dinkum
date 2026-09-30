"""URL configuration for share_dinkum_proj."""
from django.contrib import admin
from django.urls import path


from django.conf import settings
from django.conf.urls.static import static


from django.templatetags.static import static as static_url
from django.views.generic import RedirectView

# The site header, title and index title are attributes of ShareDinkumAdminSite, which is
# installed as the default admin site by ShareDinkumAdminConfig.

urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=True)),  # Redirect root URL to admin
    # Browsers request /favicon.ico from the site root whether or not a page links to it, so without
    # this every page load logs a 404. The icon itself lives in the app's static directory.
    path('favicon.ico', RedirectView.as_view(url=static_url('favicon.ico'))),
    path('admin/', admin.site.urls)
]

# Might need to remove this if this is deployed to a cloud environment
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
