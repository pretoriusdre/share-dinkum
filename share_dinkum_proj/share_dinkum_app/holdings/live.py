"""The replay as the writer: a change to a fact rebuilds its instrument's holding.

With `HOLDINGS_WRITER = 'replay'` (the default), saving or deleting a buy, sale, share split, cost
base adjustment, or an allocation entered by hand rebuilds the holding of the instrument it is
for, in the same transaction. With 'signals', the creation signals build it as records are
entered, as before 0.5.0.

* Before an instrument's first rebuild, its stored holding is checked against its trades. If any
  figure differs, the change is refused rather than letting the rebuild alter figures silently.
  `uv run dev check_holdings` lists the differences; `--rebuild` accepts them.
* A rebuild that cannot apply a decision already made (a sale of more units than its buy now
  holds, say) refuses the change.
* A record arriving already handled, from an export, is not rebuilt as it loads: the export
  carries its parcels, and the loader rebuilds once it has them all.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import threading
from typing import TYPE_CHECKING, Any

from django.conf import settings

if TYPE_CHECKING:
    from share_dinkum_app.holdings.apply import Plan
    from share_dinkum_app.models import Instrument

_state = threading.local()


class HoldingsRefused(ValueError):
    """A change the holding cannot take, with the reason."""


def active() -> bool:
    """Whether the replay writes the holdings, rather than the creation signals."""
    return str(getattr(settings, 'HOLDINGS_WRITER', 'replay')) == 'replay'


def rebuilding() -> bool:
    """Whether a rebuild is writing now, so its own saves do not start another."""
    return bool(getattr(_state, 'rebuilding', False))


@contextmanager
def writing() -> Iterator[None]:
    """While a rebuild writes: saves it makes are not changes to rebuild for."""
    previous = rebuilding()
    _state.rebuilding = True
    try:
        yield
    finally:
        _state.rebuilding = previous


def ensure_verified(instrument: 'Instrument') -> None:
    """Refuse a change to an instrument whose stored figures differ from its trades.

    Checked once, on the first change after an upgrade, and remembered on the instrument.
    """
    from share_dinkum_app.holdings import compare

    if instrument.holdings_differ is None:
        compare.verify(instrument)
    if instrument.holdings_differ:
        raise HoldingsRefused(
            f'The parcels stored for {instrument.name} give different figures from its trades, so '
            f'this change is refused rather than letting it alter them. Run '
            f'`uv run dev check_holdings --account "{instrument.account.description}"` to see the '
            f'differences, and add `--rebuild` to accept them.')


def rebuild(instrument: 'Instrument') -> 'Plan':
    """Work the instrument's holding out again and write it. Refuses a decision it cannot apply."""
    from share_dinkum_app.holdings import apply, facts, replay, state
    from share_dinkum_app.models import Instrument

    account = instrument.account
    with writing():
        replayed = replay.replay(facts.load(account, instrument))
        if replayed.problems:
            raise HoldingsRefused(' '.join(replayed.problems))
        result = apply.plan(state.stored(account, instrument), replayed.holding)
        apply.write(account, result)
    Instrument.objects.filter(pk=instrument.pk).update(holdings_differ=False)
    instrument.holdings_differ = False
    return result


def instrument_of(record: Any) -> 'Instrument | None':
    """The instrument a fact is for."""
    from share_dinkum_app import models

    if isinstance(record, models.SellAllocation):
        return record.sell.instrument if record.sell_id else None
    if isinstance(record, models.CostBaseAdjustmentAllocation):
        return record.cost_base_adjustment.instrument if record.cost_base_adjustment_id else None
    return record.instrument if getattr(record, 'instrument_id', None) else None
