from pathlib import Path
from django.conf import settings
from django.core.files.base import ContentFile



import logging
logger = logging.getLogger(__name__)


def process_filefield(value):
    # Used to handle a filefield in a data import process.

    
    # Case 0: No file provided (value is None or empty string)
    if not value:
        return None

    file_path = Path(value)

    try:
        # Case 1: Relative path under MEDIA_ROOT (re-importing existing data)
        if not file_path.is_absolute():
            media_file = Path(settings.MEDIA_ROOT) / file_path
            if media_file.exists():
                return str(file_path)  # return relative path as-is
            else:
                logger.error('Referenced local file in media directory,%s does not exist.', media_file)
                return None

        # Case 2: Absolute path (loading a new file)
        elif file_path.exists():
            # Optional: You can check here if it's *inside* MEDIA_ROOT by mistake
            if settings.MEDIA_ROOT in str(file_path.resolve()):
                # Return relative path from MEDIA_ROOT
                rel_path = file_path.relative_to(settings.MEDIA_ROOT)
                return str(rel_path)

            # Outside media root → read and return ContentFile
            with open(file_path, 'rb') as f:
                return ContentFile(f.read(), name=file_path.name)

        else:
            logger.error('File %s does not exist.', file_path)
            return None

    except Exception as e:
        logger.error('Error processing %s: %s', value, e, exc_info=True)
        return None
    



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