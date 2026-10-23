"""Whether an event read from a document is well formed, and adds up.

Run on every event, from a parser or from the AI, before it is checked against a portfolio.
"""

from decimal import Decimal

from share_dinkum_app.documents import columns
from share_dinkum_app.documents.extracted import Extracted

# The registry rounds a payment to the cent.
CENT = Decimal('0.01')


def problems(event: Extracted) -> list[str]:
    """What is wrong with `event`; empty when nothing is."""
    if event.model not in columns.EVENTS:
        return [f'{event.model!r} is not an event a document can give. It is one of: {", ".join(columns.EVENTS)}.']

    allowed = columns.columns(event.model)
    found = [f'{event.model} has no column {column!r}. Its columns are: {", ".join(allowed)}.'
             for column in event.row if column not in allowed]
    found += [f'{event.model} needs {column}, and the event has none.'
              for column in columns.REQUIRED[event.model] if event.row.get(column) is None]
    for column, value in event.row.items():
        if column in allowed and value is not None:
            problem = columns.problem_with_value(event.model, column, value)
            if problem:
                found.append(problem)
    if event.stated_total is not None and not isinstance(event.stated_total, Decimal):
        found.append(f'stated_total must be a Decimal, not {type(event.stated_total).__name__}.')
    if found:
        return found
    return total_problems(event)


PER_UNIT = {
    'Distribution': ('distribution_amount_per_share',),
    'Dividend': ('franked_amount_per_share', 'unfranked_amount_per_share'),
    'Buy': ('unit_price',),
    'Sell': ('unit_price',),
}


def worked_total(event: Extracted) -> Decimal | None:
    """The event's total before tax and fees, worked out from its per-unit figures, where it has one."""
    quantity = event.row.get('quantity')
    if quantity is None or event.model not in PER_UNIT:
        return None
    return sum((event.row[column] for column in PER_UNIT[event.model]), Decimal(0)) * quantity


def tolerance(event: Extracted) -> Decimal:
    """How far the worked total may be from the stated one: a cent, plus the rounding of each per-unit
    figure to the places the document prints, times the units. A contract note's average price of
    1.234567 may be up to 0.0000005 off, which is more than a cent over 20,000 units."""
    quantity = event.row.get('quantity') or Decimal(0)
    rounding = sum((half_last_place(event.row[column]) for column in PER_UNIT.get(event.model, ())), Decimal(0))
    return CENT + rounding * abs(quantity)


def half_last_place(value: Decimal) -> Decimal:
    """Half a unit in the last place `value` is written to: 0.0000005 for 1.234567."""
    exponent = value.as_tuple().exponent
    return Decimal(5).scaleb(exponent - 1) if isinstance(exponent, int) else Decimal(0)


def total_problems(event: Extracted) -> list[str]:
    """Whether the total the document prints agrees with the per-unit figures times the units."""
    if event.stated_total is None:
        return []
    worked = worked_total(event)
    if worked is None or abs(worked - event.stated_total) <= tolerance(event):
        return []
    return [f'The document gives a total of {event.stated_total}, but the per-unit figures times the units '
            f'come to {worked}.']
