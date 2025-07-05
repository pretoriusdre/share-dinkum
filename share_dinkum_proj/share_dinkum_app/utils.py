
def user_directory_path(instance, filename):

    parts = []
    if hasattr(instance, 'account'):
        parts.append(f'{instance.account.id}')
    if hasattr(instance, 'instrument'):
        parts.append(f'{instance.instrument.name}')

    if hasattr(instance, 'date'):
        parts.append(f'{instance.date.isoformat()}')
    elif hasattr(instance, 'created_at'):
        parts.append(f'{instance.created_at.date().isoformat()}')
    
    path_str = r'/'.join(parts)
    path_str += f'_{filename}'

    return path_str


def add_currencies(*args):
    # Disregards zero amounts which might be in a different currency
    nonzero_amounts = [arg for arg in args if arg and arg.amount != 0]
    total = nonzero_amounts[0]
    for amount in nonzero_amounts[1:]:
        total += amount
    return total
  