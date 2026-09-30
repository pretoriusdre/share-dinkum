"""The app config that installs our admin site in place of the stock one.

Not in `apps.py`, where a second AppConfig would make Django refuse to load the app.
"""

from django.contrib.admin.apps import AdminConfig


class ShareDinkumAdminConfig(AdminConfig):
    """Replaces `django.contrib.admin`, making `admin.site` a ShareDinkumAdminSite."""

    default_site = 'share_dinkum_app.admin_site.ShareDinkumAdminSite'
