
"""Automatic login for local, single-user installs.

Share Dinkum runs on your own machine against your own database, so there is nobody to keep out and
a password is only an obstacle between you and your data. This middleware signs every request in as
the local user, and creates that user on first run so that a fresh install needs no setup step at
all. Set LOCAL_AUTO_LOGIN=False in .env before letting anyone else reach an install; that removes
this middleware and restores the normal Django login page.
"""

from django.contrib.auth import get_user_model, login
from django.db import IntegrityError

# The username data_import.ipynb creates for your own data. Sharing one name means that whichever
# of the two runs first, the other finds the account already there and reuses it.
LOCAL_USERNAME = 'admin'


def _usable_superusers():
    """Superusers that can actually get into the admin, oldest first.

    is_active and is_staff matter as much as is_superuser: the admin turns either of them away at
    the door, so signing in as such a user lands on a login page it can never get past. Skipping
    them lets a usable account be found or created instead.
    """
    return get_user_model().objects.filter(
        is_superuser=True, is_active=True, is_staff=True
    ).order_by('date_joined')


def _portfolio_recency(user):
    """Sort key that opens the app on the portfolio you set up most recently.

    Whichever data you loaded last is the data you are working on, so that is what the app should
    show, in either direction: import the sample data to look at it and you get the sample data;
    import your own afterwards and you get your own. Ranking by the account each user would
    actually display, rather than by the users themselves, is what keeps this honest, because that
    is the same account the dashboard will resolve once the sign-in happens.

    Users with no portfolio at all sort below every user that has one. Among equals the caller's
    ordering decides, so the result never depends on dictionary or query ordering.
    """
    account = user.visible_account
    return (account is not None, account.created_at if account is not None else None)


class AutoLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            user = self._local_user()
            if user is not None:
                login(request, user)
        return self.get_response(request)

    @staticmethod
    def _local_user():
        """The account requests run as, or None to leave the normal login page in place.

        Looked up per request rather than once at startup, because on a brand new database there is
        no user to find until the first request creates one.
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
