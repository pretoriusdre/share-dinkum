from django.db.models.signals import post_save, post_delete, pre_save, pre_delete

import logging
logger = logging.getLogger(__name__)


def get_app_receivers(app_name):
    """
    Collect all signal receivers from the given app.
    Returns a list of tuples: (signal, receiver function, sender, dispatch_uid)
    """
    signals = [post_save, post_delete, pre_save, pre_delete]
    app_receivers = []

    for signal in signals:
        for receiver_key, receiver_weakref, _ in signal.receivers:
            func = receiver_weakref()
            if func is None:
                continue
            # Only include receivers from this app
            if func.__module__.startswith(app_name):
                # Extract sender and dispatch_uid from the receiver key
                sender = receiver_key[1] if len(receiver_key) > 1 else None
                dispatch_uid = receiver_key[2] if len(receiver_key) > 2 else None
                app_receivers.append((signal, func, sender, dispatch_uid))
    return app_receivers


def disconnect_app_signals(app_name):
    """
    Disconnects all signal receivers for the given app.
    Returns a list of tuples for later reconnection.
    """
    receivers = get_app_receivers(app_name)
    for signal, func, sender, dispatch_uid in receivers:
        signal.disconnect(func, sender=sender, dispatch_uid=dispatch_uid)
    return receivers


def reconnect_app_signals(receivers):
    """
    Reconnects a previously disconnected set of signal receivers.
    """
    for signal, func, sender, dispatch_uid in receivers:
        signal.connect(func, sender=sender, dispatch_uid=dispatch_uid)