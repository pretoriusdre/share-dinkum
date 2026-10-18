"""The portfolios a management command works on, from its `--account` option.

The leading underscore keeps Django from listing this as a command.
"""

import uuid

from django.core.management.base import CommandError
from django.db.models import QuerySet

from share_dinkum_app.models import Account


def select_accounts(name: str | None) -> 'QuerySet[Account]':
    """The portfolios `--account NAME` means, oldest first: every one when NAME is omitted.

    NAME is a portfolio's name, `owner/name`, or its id. A name is unique only for one owner, so
    one that two owners use is refused, naming both, rather than run on each.
    """
    accounts = Account.objects.order_by('created_at')
    if not name:
        return accounts
    try:
        matched = accounts.filter(pk=uuid.UUID(name))
    except ValueError:
        matched = accounts.filter(description=name)
        owner, _, description = name.rpartition('/')
        if owner and not matched.exists():
            matched = accounts.filter(owner__username=owner, description=description)
    if not matched.exists():
        raise CommandError(f'No portfolio named "{name}".')
    if matched.count() > 1:
        named = ', '.join(
            f'{account.owner.get_username()}/{account.description}'
            for account in matched.select_related('owner'))
        raise CommandError(
            f'More than one portfolio is named "{name}": {named}. Give it as owner/name.')
    return matched
