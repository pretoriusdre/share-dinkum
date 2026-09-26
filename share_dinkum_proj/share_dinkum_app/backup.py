"""Backing up the database and attached documents.

Shared by `uv run update`, the dashboard and the notebook's `DataBackupManager`. Backups are
written to `<root>/<name>/<timestamp>/` and pruned to the most recent five.

* No Django imports: `devserver.py` backs up before an update, when the app may not boot.
* The root is in the home directory, so a backup survives losing the project folder.
"""

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

DEFAULT_BACKUP_ROOT = Path.home() / 'share-dinkum-backups'

#: Sorts chronologically as text; includes seconds so backups in one minute do not collide.
BACKUP_FOLDER_FORMAT = '%Y-%m-%dT%H%M%S'

#: Backups kept per set.
RETAIN_BACKUPS = 5

#: The set a backup goes into unless the caller says otherwise.
DEFAULT_NAME = 'main'


def copy_sqlite_database(source, destination):
    """Copy a SQLite database with its online backup API, which is safe while it is in use."""
    source_connection = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            with destination_connection:
                source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def set_path(root, name=DEFAULT_NAME):
    return Path(root or DEFAULT_BACKUP_ROOT) / name


def list_backups(root=None, name=DEFAULT_NAME):
    """Backup folder names in a set, newest first."""
    base = set_path(root, name)
    if not base.exists():
        return []
    return sorted((folder.name for folder in base.iterdir() if folder.is_dir()), reverse=True)


def legacy_backups(root=None):
    """Old-layout backups (`<root>/<timestamp>/`), newest first. Never pruned."""
    root = Path(root or DEFAULT_BACKUP_ROOT)
    if not root.exists():
        return []
    found = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            datetime.strptime(folder.name, BACKUP_FOLDER_FORMAT)
        except ValueError:
            continue  # A named set, or something a person put there.
        found.append(folder)
    return sorted(found, key=lambda path: path.name, reverse=True)


def latest_backup(root=None, name=DEFAULT_NAME):
    """The path of the most recent backup in the set or the old layout, or None."""
    candidates = [set_path(root, name) / folder for folder in list_backups(root, name)]
    candidates += legacy_backups(root)
    return max(candidates, key=lambda path: path.name) if candidates else None


def cleanup_old_backups(root=None, name=DEFAULT_NAME, keep=RETAIN_BACKUPS):
    """Delete all but the newest `keep` backups in a set. Returns the names removed.

    A folder that fails to delete is skipped, not raised.
    """
    base = set_path(root, name)
    if not base.exists():
        return []

    removed = []
    for folder_name in list_backups(root, name)[keep:]:
        try:
            shutil.rmtree(base / folder_name)
            removed.append(folder_name)
        except OSError:
            pass
    return removed


def make_backup(database, media, root=None, name=DEFAULT_NAME, keep=RETAIN_BACKUPS):
    """Copy the database and media folder into a new timestamped folder, then prune.

    Returns `{path, database_bytes, media_files, removed}`, or None if neither exists.
    """
    database, media = Path(database), Path(media)
    if not database.exists() and not media.exists():
        return None

    destination = set_path(root, name) / datetime.now().strftime(BACKUP_FOLDER_FORMAT)
    destination.mkdir(parents=True, exist_ok=True)

    result = {'path': destination, 'database_bytes': 0, 'media_files': 0, 'removed': []}

    if database.exists():
        copy_sqlite_database(database, destination / database.name)
        result['database_bytes'] = database.stat().st_size

    if media.exists():
        shutil.copytree(media, destination / media.name, dirs_exist_ok=True)
        result['media_files'] = sum(1 for path in media.rglob('*') if path.is_file())

    result['removed'] = cleanup_old_backups(root, name, keep)
    return result
