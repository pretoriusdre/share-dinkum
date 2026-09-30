
"""Automatic login for local, single-user installs.

Signs every request in as a local superuser, creating one on first run. Set
LOCAL_AUTO_LOGIN=False in .env to remove it and restore the normal login page.
"""

from collections.abc import Callable
from typing import Any

from django.contrib.auth import get_user_model, login
from django.db import IntegrityError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

# The username data_import.ipynb creates for your own data. Sharing one name means that whichever
# of the two runs first, the other finds the account already there and reuses it.
LOCAL_USERNAME = 'admin'


def _usable_superusers() -> QuerySet[Any]:
    """Active staff superusers (the ones the admin accepts), oldest first."""
    return get_user_model().objects.filter(
        is_superuser=True, is_active=True, is_staff=True
    ).order_by('date_joined')


def _portfolio_recency(user: Any) -> tuple[bool, Any]:
    """Sort key ranking a user by when their visible portfolio was created.

    Picks the user whose portfolio was set up most recently. Users with none rank lowest.
    """
    account = user.visible_account
    return (account is not None, account.created_at if account is not None else None)


class AutoLoginMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            user = self._local_user()
            if user is not None:
                login(request, user)
        return self.get_response(request)

    @staticmethod
    def _local_user() -> Any:
        """The user to sign in as, created if none exists, or None to show the login page.

        Looked up per request, since a new database has no user until the first request.
        """
        usable = list(_usable_superusers())
        if usable:
            return max(usable, key=_portfolio_recency)

        try:
            # No password is set, so this account cannot be used to log in from anywhere else.
            # Deliberately not wrapped in transaction.atomic(): that would hold SQLite's write lock
            # for the whole of create_superuser, so simultaneous first requests would wait out the
            # lock timeout instead of losing the race cheaply on the unique constraint below.
            return get_user_model().objects.create_superuser(username=LOCAL_USERNAME, password=None)
        except IntegrityError:
            # Either a request that arrived at the same moment created it first, or the name is
            # taken by an account that has been deactivated on purpose. A fresh query finds the
            # former, and returns None for the latter so the login page appears as intended. It
            # has to be a new queryset: reusing the one above would answer from its result cache,
            # which was filled before the other request created the user.
            return _usable_superusers().first()
