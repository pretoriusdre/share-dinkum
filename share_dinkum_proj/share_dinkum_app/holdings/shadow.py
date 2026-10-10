"""Shadow mode: the signals still write the holding, and the replay checks they agree.

Before the replay writes holdings itself, it has to plan no change for every holding the signals
build. `HOLDINGS_SHADOW` in settings says what a disagreement does:

* 'off' (the default): nothing is checked.
* 'log': a warning is logged, at the end of an import.
* 'assert': it raises. The test runner sets this, and checks every portfolio after each test.
"""

import logging
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from share_dinkum_app.holdings.apply import Plan
    from share_dinkum_app.models import Account

logger = logging.getLogger(__name__)


class HoldingsDisagree(AssertionError):
    """The replay would change a holding the signals built."""


def mode() -> str:
    return str(getattr(settings, 'HOLDINGS_SHADOW', 'off'))


def plan_for(account: 'Account') -> 'Plan':
    from share_dinkum_app.holdings import apply, facts, replay, state
    return apply.plan(state.stored(account), replay.replay(facts.load(account)).holding)


def check(account: 'Account', where: str) -> None:
    """Plan a rebuild of `account`, and report a non-empty plan as `mode()` says."""
    if mode() == 'off':
        return
    result = plan_for(account)
    if result.empty:
        return
    from share_dinkum_app.holdings import compare
    figures = compare.check(account).count
    message = (f'After {where}, a rebuild of {account} would change: {result.summary()}. '
               f'{figures} figure(s) differ.')
    if mode() == 'assert':
        raise HoldingsDisagree(message)
    logger.warning(message)
