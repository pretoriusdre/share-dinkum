"""Which portfolio a request is about, and the views that change or depend on it.

The selected portfolio is the user's `default_account`, read through `AppUser.visible_account`
so that one the user may not see is passed over. The dashboard, the admin lists and the media
view all follow it. A superuser may select any portfolio, anyone else only their own
(`AppUser.portfolios`).
"""

import posixpath
from typing import TYPE_CHECKING, Any
import uuid

from django.conf import settings
from django.contrib import messages
from django.db.models import Model
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.static import serve

if TYPE_CHECKING:
    from share_dinkum_app.models import Account

_UNSET = object()
_CACHE_ATTRIBUTE = '_share_dinkum_portfolio'


def is_scoped(model: type[Model]) -> bool:
    """Whether each row of `model` belongs to one portfolio (an `account` field), as all but the
    shared tables do."""
    from share_dinkum_app.models import Account

    return model is not Account and any(
        field.name == 'account' for field in model._meta.concrete_fields)


def current_portfolio(request: HttpRequest) -> 'Account | None':
    """The portfolio selected by the user making `request`, or None if there is none.

    Kept on the request, since an admin page asks for it once per list, field and inline.
    """
    cached = getattr(request, _CACHE_ATTRIBUTE, _UNSET)
    if cached is _UNSET:
        cached = getattr(request.user, 'visible_account', None)
        setattr(request, _CACHE_ATTRIBUTE, cached)
    return cached  # type: ignore[return-value]


def select(request: HttpRequest, account: 'Account') -> None:
    """Make `account` the selected portfolio of the user making `request`."""
    user: Any = request.user
    user.default_account = account
    user.save(update_fields=['default_account'])
    setattr(request, _CACHE_ATTRIBUTE, account)


def _redirect_target(request: HttpRequest, next_url: str | None) -> str:
    """Where to go after switching: `next` if it is this site's, as its list if it was one record.

    A record's page would only say that it is in the other portfolio.
    """
    dashboard = reverse('admin:dashboard')
    if not next_url or not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return dashboard
    try:
        match = resolve(next_url.split('?')[0])
    except Resolver404:
        return dashboard
    if match.url_name and match.url_name.endswith(('_change', '_delete', '_history')):
        try:
            return reverse(f'admin:{match.url_name.rsplit("_", 1)[0]}_changelist')
        except NoReverseMatch:  # Not a record's page, such as `password_change`.
            return next_url
    return next_url


@require_POST
def switch_portfolio_view(request: HttpRequest) -> HttpResponse:
    """Select the posted portfolio, if the user may see it, and go back to where they were."""
    user: Any = request.user
    try:
        account = user.portfolios().filter(pk=request.POST.get('account')).first()
    except Exception:  # Not a portfolio id at all.
        account = None
    if account is None:
        messages.error(request, 'That portfolio is not one you can select.')
    else:
        select(request, account)
        messages.success(request, f'Showing {account.description}.')
    return HttpResponseRedirect(_redirect_target(request, request.POST.get('next')))


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """A document or export, for a user who may see the portfolio it belongs to.

    Files are kept under a folder named by the portfolio's id (`user_directory_path`), so that
    folder decides. Anything else in the media folder is not Share Dinkum's, and is not served.
    """
    user: Any = request.user
    # On Windows `serve` reads `\` as a separator too, so `<id>/..\<other id>/...` would reach another
    # portfolio's folder. No file Share Dinkum stores has one in its path.
    if '\\' in path:
        raise Http404('No such document.')
    # As `serve` will read it, so `<id>/../<other id>/...` is judged by the folder it reaches.
    path = posixpath.normpath(path).lstrip('/')
    try:
        account_id = uuid.UUID(path.split('/')[0])
    except ValueError:
        raise Http404('No such document.')
    if not user.portfolios().filter(pk=account_id).exists():
        raise Http404('No such document.')
    return serve(request, path, document_root=settings.MEDIA_ROOT)
