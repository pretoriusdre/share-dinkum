"""The trades an option exercise stands for: a sale of the options, and buys of the shares.

An exercise is entered as one record, but holdings are worked out from buys and sales, so it writes
those and the replay does the rest (`live`). The sale takes options by its strategy as any sale
does; its price is nothing, and its gain is no capital gain (`SellAllocation.total_capital_gain`).
There is one buy of the shares per option buy the sale took from, so options bought at different
prices give shares with different cost bases (`Buy.option_cost_base`).

The trades belong to the exercise. They change when it does and go when it is deleted; changed or
deleted on their own, they are refused (`writing`).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
import threading
from typing import TYPE_CHECKING, Any

from djmoney.money import Money

from share_dinkum_app.holdings import live

if TYPE_CHECKING:
    from share_dinkum_app.models import OptionExercise, Sell

_state = threading.local()

QUANTITY_PLACES = Decimal('0.0001')
AMOUNT_PLACES = Decimal('0.0001')


def writing_now() -> bool:
    """Whether an exercise is writing its trades now, so changing them is not refused."""
    return bool(getattr(_state, 'writing', False))


@contextmanager
def writing() -> Iterator[None]:
    previous = writing_now()
    _state.writing = True
    try:
        yield
    finally:
        _state.writing = previous


def _units(value: Decimal) -> str:
    return f'{value.normalize():f}'


def _shares(total: Decimal, weights: list[Decimal], places: Decimal) -> list[Decimal]:
    """`total` divided in proportion to `weights`, each rounded to `places`, the last taking what
    rounding leaves so they add up to it exactly."""
    whole = sum(weights, Decimal('0'))
    parts = [(total * weight / whole).quantize(places, rounding=ROUND_HALF_UP) for weight in weights[:-1]]
    return [*parts, total - sum(parts, Decimal('0'))]


def write(exercise: 'OptionExercise') -> None:
    """Make the exercise's sale and buys what it says, in the caller's transaction.

    Refused if fewer options were held on the day than it exercises.
    """
    from django.db import transaction

    if not live.active():
        raise live.HoldingsRefused(
            'An option exercise is worked out by the holdings replay, which is switched off '
            '(HOLDINGS_WRITER is not "replay").')
    with writing(), transaction.atomic():
        sale = _write_sale(exercise)
        taken = _taken(sale)
        held = sum((units for _, units in taken), Decimal('0'))
        if held < exercise.quantity:
            raise live.HoldingsRefused(
                f'Only {_units(held)} of {exercise.instrument.name} were held on {exercise.date}, '
                f'so {_units(exercise.quantity)} cannot be exercised.')
        _write_buys(exercise, taken)


def differences(exercise: 'OptionExercise') -> list[str]:
    """How the exercise's trades differ from what it says; empty if they agree.

    Nothing writes them out of step, but a file loaded under the replay's own writer is taken as
    it stands (`DataLoader`), so an exercise edited in an export arrives with its old trades.
    """
    from share_dinkum_app.models import Buy, Sell

    found: list[str] = []
    sale = Sell.objects.filter(exercise=exercise).first()
    if sale is None:
        return ['it has no sale of the options']
    if (sale.instrument_id, sale.date, sale.quantity, sale.strategy) != (
            exercise.instrument_id, exercise.date, exercise.quantity, exercise.strategy):
        found.append('its sale of the options is for another instrument, date, quantity or strategy')
    buys = list(Buy.objects.filter(exercise=exercise))
    if sum((buy.quantity for buy in buys), Decimal('0')) != exercise.shares_received:
        found.append('its buys of the shares are for another number of shares')
    price = exercise.share_price()
    if any((buy.instrument_id, buy.date, buy.unit_price) != (exercise.underlying_instrument_id, exercise.date, price)
           for buy in buys):
        found.append('a buy of the shares is for another instrument, date or price')
    return found


def remove(exercise: 'OptionExercise') -> None:
    """Delete the exercise's trades, shares first. Refused once any of its shares are sold."""
    from django.db import transaction
    from share_dinkum_app.models import Buy, Sell

    with writing(), transaction.atomic():
        for buy in Buy.objects.filter(exercise=exercise):
            _refuse_if_sold(buy, 'deleted')
            buy.delete()
        sale = Sell.objects.filter(exercise=exercise).first()
        if sale is not None:
            _delete_sale(sale)


def _refuse_if_sold(buy: Any, doing: str) -> None:
    from share_dinkum_app.models import SellAllocation

    sold = SellAllocation.objects.filter(parcel__buy=buy, is_active=True).select_related('sell').first()
    if sold is not None:
        raise live.HoldingsRefused(
            f'Shares from this exercise were sold on {sold.sell.date}, so it cannot be {doing}. '
            f'Delete that sale first.')


def _delete_sale(sale: 'Sell') -> None:
    """Delete the sale of options and its allocations, then work the options out again."""
    from share_dinkum_app.models import SellAllocation

    instrument = sale.instrument
    with live.writing():
        SellAllocation.objects.filter(sell=sale).delete()
        sale.delete()
    live.rebuild(instrument)


def _write_sale(exercise: 'OptionExercise') -> 'Sell':
    """The sale of the options, written or rewritten to match the exercise."""
    from share_dinkum_app.models import Sell

    sale = Sell.objects.filter(exercise=exercise).first()
    if sale is not None and (
            sale.instrument_id, sale.date, sale.quantity, sale.strategy) == (
            exercise.instrument_id, exercise.date, exercise.quantity, exercise.strategy):
        return sale
    if sale is not None:
        # A sale's date and quantity are fixed once it has taken its parcels, and those were for
        # the old ones. It is entered again, so the replay takes the options afresh.
        _delete_sale(sale)
    # In the portfolio currency, which needs no exchange rate for an amount of nothing.
    nothing = Money(Decimal('0'), str(exercise.account.currency))
    sale = Sell(
        account=exercise.account, exercise=exercise, instrument=exercise.instrument,
        date=exercise.date, quantity=exercise.quantity, strategy=exercise.strategy,
        unit_price=nothing, total_brokerage=nothing)
    sale.save()
    return sale


def _taken(sale: 'Sell') -> list[tuple[Any, Decimal]]:
    """(option buy id, units) for each buy the sale took options from, oldest buy first."""
    from django.db.models import Sum
    from share_dinkum_app.models import SellAllocation

    rows = (SellAllocation.objects.filter(sell=sale, is_active=True)
            .values('parcel__buy_id', 'parcel__buy__date')
            .annotate(units=Sum('quantity')))
    ordered = sorted(rows, key=lambda row: (row['parcel__buy__date'], str(row['parcel__buy_id'])))
    return [(row['parcel__buy_id'], row['units']) for row in ordered]


def _write_buys(exercise: 'OptionExercise', taken: list[tuple[Any, Decimal]]) -> None:
    """One buy of the shares per option buy taken, written or rewritten to match the exercise.

    The shares and fees are divided between them by the options each took.
    """
    from share_dinkum_app.models import Buy

    existing = {buy.option_buy_id: buy for buy in Buy.objects.filter(exercise=exercise)}
    weights = [units for _, units in taken]
    shares = _shares(exercise.shares_received, weights, QUANTITY_PLACES)
    fees = _shares(exercise.total_brokerage.amount, weights, AMOUNT_PLACES)
    price = exercise.share_price()

    for (option_buy_id, _), quantity, fee in zip(taken, shares, fees):
        wanted = {
            'instrument_id': exercise.underlying_instrument_id,
            'date': exercise.date,
            'quantity': quantity,
            'unit_price': price,
            'total_brokerage': Money(fee, exercise.total_brokerage.currency),
        }
        buy = existing.pop(option_buy_id, None)
        if buy is not None and (str(buy.unit_price.currency), str(buy.total_brokerage.currency)) != (
                str(price.currency), str(exercise.total_brokerage.currency)):
            # A buy's currencies are fixed once it has its parcel.
            _refuse_if_sold(buy, 'changed to another currency')
            buy.delete()
            buy = None
        if buy is None:
            Buy(account=exercise.account, exercise=exercise, option_buy_id=option_buy_id, **wanted).save()
            continue
        if any(getattr(buy, name) != value for name, value in wanted.items()):
            for name, value in wanted.items():
                setattr(buy, name, value)
            buy.save()

    for buy in existing.values():
        _refuse_if_sold(buy, 'changed to take no options from that buy')
        buy.delete()
