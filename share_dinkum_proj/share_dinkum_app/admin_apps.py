"""The app config that installs our admin site in place of the stock one.

Its own module rather than sitting in `apps.py`. Django scans a package's `apps` module for
AppConfig subclasses to find the app's default, and an `AdminConfig` imported there counts as
one -- two candidates in one module makes the app refuse to load at all
("declares more than one default AppConfig"). Keeping this separate leaves `apps.py` with
exactly one.
"""

from django.contrib.admin.apps import AdminConfig


class ShareDinkumAdminConfig(AdminConfig):
    """Installed in place of `django.contrib.admin`, so `admin.site` is our own site.

    Naming the site here rather than instantiating one ourselves is what keeps every
    existing `admin.site.register(...)` working: `admin.site` *becomes* the subclass, so
    there is only ever one site and nothing has to be registered twice.
    """

    default_site = 'share_dinkum_app.admin_site.ShareDinkumAdminSite'
