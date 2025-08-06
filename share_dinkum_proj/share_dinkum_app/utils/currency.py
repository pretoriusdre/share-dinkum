from djmoney.money import Money


def add_currencies(*args):
    # Disregards zero amounts which might be in a different currency
    nonzero_amounts = [arg for arg in args if arg and arg.amount != 0]

    if len(nonzero_amounts) == 0:
        return Money(0, nonzero_amounts[0].currency if nonzero_amounts else 'AUD')  # Default to AUD if no amounts
    
    total = nonzero_amounts[0]
    for amount in nonzero_amounts[1:]:
        total += amount
    return total
  