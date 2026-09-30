# decorators.py


from collections.abc import Callable
from functools import wraps
from typing import Any



def safe_property(func: Callable[[Any], Any]) -> property:
    """Like @property, but returns None while the instance is being added (e.g. admin add view)."""
    @wraps(func)
    def getter(self: Any) -> Any:
        if getattr(self._state, 'adding', False):
            return None
        return func(self)

    # Mark this property so signals can detect it
    setattr(getter, '_is_safe_property', True)

    return property(getter)
