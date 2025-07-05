"""
URL configuration for share_dinkum_proj project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path


from django.conf import settings
from django.conf.urls.static import static


from django.views.generic import RedirectView

# Adds site header, site title, index title to the admin side.
admin.site.site_header = 'Share Dinkum'
admin.site.site_title = 'Share Dinkum'
admin.site.index_title = 'Share Dinkum. An open-source share tracker.'



urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=True)),  # Redirect root URL to admin
    path('admin/', admin.site.urls)
]

# Might need to remove this if this is deployed to a cloud environment
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
