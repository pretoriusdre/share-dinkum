"""The admin site itself, so the dashboard is part of it rather than bolted on.

This replaces two blocks that reassigned `admin.site.get_urls` and `admin.site.index` at
import time, each guarded by a flag on the site object to stop a second import doing it
twice. That worked, but it left the dashboard depending on module import order, and every
new action had to be threaded through a closure over the original `get_urls`. Subclassing
gives both as ordinary overrides, and the guards stop being necessary because nothing is
being mutated in place.

`share_dinkum_app.apps.ShareDinkumAdminConfig` names this class as `default_site`, which
makes `admin.site` an instance of it. Every existing `admin.site.register(...)` therefore
keeps working untouched -- nothing needs re-registering against a second site.
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
        """Admin URLs, with the dashboard and one route per declared action.

        Imported inside the method rather than at module level. This class is named by
        `AdminConfig.default_site` and so is imported very early -- before the app registry
        is populated -- while `dashboard` reaches models and would fail if pulled in at that
        point.
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
