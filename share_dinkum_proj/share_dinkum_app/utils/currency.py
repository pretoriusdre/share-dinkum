from djmoney.money import Money

from share_dinkum_app.constants import DEFAULT_CURRENCY


def add_currencies(*amounts: Money, default_currency: str = DEFAULT_CURRENCY) -> Money:
    """Sum Money amounts, ignoring zeros (whatever their currency).

    Raises TypeError for a non-Money, ValueError for mixed non-zero currencies. Returns
    `Money(0, default_currency)` if nothing is non-zero.
    """
    # Filter out invalid or zero amounts
    nonzero_amounts: list[Money] = []
    for amt in amounts:
        if not isinstance(amt, Money):
            raise TypeError(f"Expected Money, got {type(amt).__name__}")
        if amt.amount != 0:
            nonzero_amounts.append(amt)
    
    if not nonzero_amounts:
        # Nothing to sum, return 0 in default currency
        return Money(0, default_currency)
    
    # Check all currencies match
    first_currency = nonzero_amounts[0].currency
    for amt in nonzero_amounts[1:]:
        if amt.currency != first_currency:
            raise ValueError(
                f"Cannot add different currencies: {first_currency} vs {amt.currency}"
            )
    
    # Sum amounts
    total = Money(0, first_currency)
    for amt in nonzero_amounts:
        total += amt
    
    return total