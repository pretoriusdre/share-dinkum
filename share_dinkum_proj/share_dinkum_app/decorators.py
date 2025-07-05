# decorators.py


from functools import wraps



def safe_property(func):
    """
    Combined decorator: behaves like @property but returns None when the instance is unsaved.

    Useful for model properties that shouldn't run logic when the object is in the Django admin 'add' view.

    Example:
        @safe_property
        def sale_date(self):
            return self.sell.date
    """
    @property
    @wraps(func)
    def wrapper(self):
        if getattr(self._state, 'adding', False):
            return None
        return func(self)
    return wrapper