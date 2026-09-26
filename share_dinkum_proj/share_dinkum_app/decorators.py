# decorators.py


from functools import wraps



def safe_property(func):
    """Like @property, but returns None while the instance is being added (e.g. admin add view)."""
    @property
    @wraps(func)
    def wrapper(self):
        if getattr(self._state, 'adding', False):
            return None
        return func(self)
    
    # Mark this property so signals can detect it
    wrapper.fget._is_safe_property = True
    
    return wrapper