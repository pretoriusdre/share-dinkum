from pathlib import Path
from django.conf import settings
from django.core.files.base import ContentFile


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
                print(f"File {media_file} does not exist.")
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
            print(f"File {file_path} does not exist.")
            return None

    except Exception as e:
        print(f"Error processing {value}: {e}")
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