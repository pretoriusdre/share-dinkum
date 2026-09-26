"""The admin site, with the dashboard as its index.

`admin_apps.ShareDinkumAdminConfig` sets this as `default_site`, so `admin.site` is an
instance of it and `admin.site.register(...)` works as usual.
"""

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path


class ShareDinkumAdminSite(admin.AdminSite):
    """The Django admin, plus the dashboard and its actions."""

    site_header = 'Share Dinkum'
    site_title = 'Share Dinkum'
    index_title = 'Share Dinkum. An open-source share tracker.'

    def get_urls(self):
        """Admin URLs plus the dashboard and one route per dashboard action.

        `dashboard` is imported here, as this module loads before the app registry is ready.
        """
        from share_dinkum_app.dashboard import DASHBOARD_ACTIONS, dashboard_view

        custom_urls = [
            path('dashboard/', self.admin_view(dashboard_view), name='dashboard'),
        ]
        custom_urls += [
            path(
                f'dashboard/{action.route}',
                self.admin_view(action.view),
                name=action.url_name,
            )
            for action in DASHBOARD_ACTIONS
        ]
        # Custom URLs first, so a model never named `dashboard` could shadow them.
        return custom_urls + super().get_urls()

    def index(self, request, extra_context=None):
        """The admin index is the dashboard."""
        from share_dinkum_app.dashboard import prepare_dashboard_context

        app_list = self.get_app_list(request)
        context = {
            **self.each_context(request),
            'title': self.index_title,
            'subtitle': None,
            'app_list': app_list,
            'available_apps': app_list,
        }
        if extra_context:
            context.update(extra_context)
        prepare_dashboard_context(request, context)
        request.current_app = self.name
        return TemplateResponse(request, self.index_template or 'admin/dashboard.html', context)
