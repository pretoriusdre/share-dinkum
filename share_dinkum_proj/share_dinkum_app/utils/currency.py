from djmoney.money import Money

from share_dinkum_app.constants import DEFAULT_CURRENCY


def add_currencies(*amounts, default_currency=DEFAULT_CURRENCY):
    """
    Sum multiple Money objects safely.
    
    - Ignores zero amounts.
    - Raises an error if nonzero amounts have different currencies.
    - Returns Money(0, default_currency) if all amounts are zero or no amounts provided.
    """
    # Filter out invalid or zero amounts
    nonzero_amounts = []
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