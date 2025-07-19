def add_currencies(*args):
    # Disregards zero amounts which might be in a different currency
    nonzero_amounts = [arg for arg in args if arg and arg.amount != 0]
    total = nonzero_amounts[0]
    for amount in nonzero_amounts[1:]:
        total += amount
    return total
  