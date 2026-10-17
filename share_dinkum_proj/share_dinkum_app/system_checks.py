"""Startup checks on where files are kept, shown by `runserver`, `check` and `migrate`."""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core import checks

from share_dinkum_app import backup


@checks.register()
def media_root(app_configs: Any, **kwargs: Any) -> list[checks.CheckMessage]:
    """Warn about the media folder: missing, left behind in the default place, or not only ours."""
    media = Path(settings.MEDIA_ROOT)
    messages: list[checks.CheckMessage] = []

    if not media.exists():
        if media.parent.exists():
            messages.append(checks.Warning(
                f'The media folder ({media}) does not exist yet.',
                hint='It is created when the first export or document is saved.',
                id='share_dinkum_app.W001',
            ))
        else:
            messages.append(checks.Warning(
                f'The media folder ({media}) cannot be created: {media.parent} does not exist.',
                hint='Check MEDIA_LOCATION in share_dinkum_proj/.env, including the drive letter.',
                id='share_dinkum_app.W002',
            ))

    default = Path(settings.BASE_DIR) / 'media'
    if media.resolve() != default.resolve() and default.is_dir() and any(default.iterdir()):
        messages.append(checks.Warning(
            f'The media folder ({media}) is not the default one, and {default} still holds files.',
            hint=f'Move the contents of {default} to {media}, or attached documents and exports will not be found.',
            id='share_dinkum_app.W003',
        ))

    try:
        foreign = backup.foreign_media_entries(media)
    except OSError:
        foreign = []  # An unreadable folder is for the first export to report.
    if foreign:
        messages.append(checks.Warning(
            f'The media folder ({media}) holds {backup.describe_names(foreign)}, which Share Dinkum did not put there.',
            hint='Every backup copies it, and a restore or clear-all leaves it alone. '
                 'Move those files out if you do not want them backed up.',
            id='share_dinkum_app.W004',
        ))

    return messages
