"""Backing up the database and attached documents.

Shared by `uv run update.py`, the dashboard and the notebook's `DataBackupManager`. Backups are
written to `<root>/<name>/<timestamp>/` and pruned to the most recent five.

* No Django imports: `devserver.py` backs up before an update, when the app may not boot.
* The root is a `share-dinkum-backups` folder inside `BACKUP_LOCATION` (the home directory unless
  set), so a backup survives losing the project folder, and pruning only reaches folders made here.
"""

from datetime import datetime
from os import PathLike
from pathlib import Path
import re
import shutil
import sqlite3
from typing import TypedDict

# A path given as text or as a Path, since callers pass both.
PathInput = str | PathLike[str]


class BackupResult(TypedDict):
    path: Path
    database_bytes: int
    media_files: int
    removed: list[str]

#: The folder backups go into, made inside `BACKUP_LOCATION`. Only this folder is ever written to
#: or pruned, so the location can be any folder, such as Documents.
BACKUPS_FOLDER_NAME = 'share-dinkum-backups'

DEFAULT_BACKUP_ROOT = Path.home() / BACKUPS_FOLDER_NAME

#: What a backup calls its copy of the media folder, whatever the folder itself is called, so that
#: a restore knows where to look.
MEDIA_FOLDER_NAME = 'media'

#: Sorts chronologically as text; includes seconds so backups in one minute do not collide.
BACKUP_FOLDER_FORMAT = '%Y-%m-%dT%H%M%S'

#: Backups kept per set.
RETAIN_BACKUPS = 5

#: The set a backup goes into unless the caller says otherwise.
DEFAULT_NAME = 'main'


def configured_database(project: PathInput) -> Path | None:
    """The SQLite file the app uses, read from the project's `.env` the way settings does.

    An environment variable wins over `.env`, as in settings. Returns None for a database that is
    not a SQLite file (another engine, ':memory:' or a 'file:' URI), which this cannot copy.
    """
    from decouple import Config, RepositoryEmpty, RepositoryEnv

    project = Path(project)
    env_file = project / '.env'
    config = Config(RepositoryEnv(str(env_file)) if env_file.exists() else RepositoryEmpty())

    engine = str(config('DB_ENGINE', default='django.db.backends.sqlite3'))
    name = str(config('DB_NAME', default='db.sqlite3'))
    if 'sqlite' not in engine or name.startswith((':', 'file:')):
        return None
    return Path(name) if Path(name).is_absolute() else project / name


def configured_folder(project: PathInput, key: str, default: Path) -> Path:
    """A folder named by `key` in the environment or the project's `.env`, else `default`.

    Read the way `configured_database` reads `DB_NAME`. A blank value counts as unset, a leading
    `~` is expanded, and a relative path is taken from the project folder.
    """
    from decouple import Config, RepositoryEmpty, RepositoryEnv

    project = Path(project)
    env_file = project / '.env'
    config = Config(RepositoryEnv(str(env_file)) if env_file.exists() else RepositoryEmpty())

    value = str(config(key, default='')).strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if path.drive and not path.root:
        # On Windows 'D:' means the current folder on D, which is not what anyone writing it means.
        path = Path(path.drive + '\\', *path.parts[1:])
    return path if path.is_absolute() else project / path


def configured_backup_root(project: PathInput) -> Path:
    """The folder backups go into: a `share-dinkum-backups` folder inside `BACKUP_LOCATION`.

    `BACKUP_LOCATION` is where that folder lives, not the folder itself, and is the home folder
    unless set, so the default is `~/share-dinkum-backups`.
    """
    return configured_folder(project, 'BACKUP_LOCATION', Path.home()) / BACKUPS_FOLDER_NAME


#: The app files each document under a folder named by its account's id, so the top of the media
#: folder holds nothing else.
ACCOUNT_FOLDER = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

#: Files an operating system or a sync client adds to any folder. They are not anyone's data.
IGNORED_FILES = frozenset({'desktop.ini', 'thumbs.db', '.ds_store'})


def foreign_media_entries(media: PathInput) -> list[str]:
    """Names at the top of the media folder that are not an account's folder, sorted.

    Share Dinkum puts only folders named by account id there (see `user_directory_path`), so
    anything else is someone's other files, in a folder called `media` that was already there.
    A missing folder has none.
    """
    media = Path(media)
    if not media.is_dir():
        return []
    return sorted(
        entry.name for entry in media.iterdir()
        if entry.name.lower() not in IGNORED_FILES and not (entry.is_dir() and ACCOUNT_FOLDER.match(entry.name))
    )


def empty_media_folder(media: PathInput) -> list[str]:
    """Delete the account folders in the media folder, and nothing else. Returns names it could not delete.

    Clearing all data and restoring a backup replace what Share Dinkum put in the media folder.
    Only the account folders are its own, so anything beside them, such as someone's folder that
    was already called `media`, is left alone, and so is the media folder itself. A folder that
    fails to delete (an export open in Excel, say) is skipped and its name returned.
    """
    media = Path(media)
    if not media.is_dir():
        return []
    failed: list[str] = []
    for entry in media.iterdir():
        is_link = entry.is_symlink() or entry.is_junction()
        if entry.is_dir() and not is_link and ACCOUNT_FOLDER.match(entry.name):
            try:
                shutil.rmtree(entry)
            except OSError:
                failed.append(entry.name)
    return sorted(failed)


def describe_names(names: list[str], shown: int = 5) -> str:
    """Names for a message: the first few, quoted, then how many more."""
    text = ', '.join(f"'{name}'" for name in names[:shown])
    return f'{text} and {len(names) - shown} more' if len(names) > shown else text


def configured_media_root(project: PathInput) -> Path:
    """The folder for exports and attached documents: a `media` folder inside `MEDIA_LOCATION`.

    `MEDIA_LOCATION` is where that folder lives, not the folder itself, and is the project folder
    unless set. Share Dinkum only ever copies, empties and restores the `media` folder it names,
    so a location such as a documents folder, a home folder or a drive puts no other files at risk.
    """
    return configured_folder(project, 'MEDIA_LOCATION', Path(project)) / MEDIA_FOLDER_NAME


def copy_sqlite_database(source: PathInput, destination: PathInput) -> None:
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


def set_path(root: PathInput | None, name: str = DEFAULT_NAME) -> Path:
    return Path(root or DEFAULT_BACKUP_ROOT) / name


def _is_backup_name(name: str) -> bool:
    """Whether a folder is named like a backup. Anything else in the backup folder is not ours."""
    try:
        datetime.strptime(name, BACKUP_FOLDER_FORMAT)
    except ValueError:
        return False
    return True


def list_backups(root: PathInput | None = None, name: str = DEFAULT_NAME) -> list[str]:
    """Backup folder names in a set, newest first. Folders not named like a backup are not listed,
    so pruning never deletes them."""
    base = set_path(root, name)
    if not base.exists():
        return []
    return sorted((folder.name for folder in base.iterdir() if folder.is_dir() and _is_backup_name(folder.name)),
                  reverse=True)


def legacy_backups(root: PathInput | None = None) -> list[Path]:
    """Old-layout backups (`<root>/<timestamp>/`), newest first. Never pruned."""
    root = Path(root or DEFAULT_BACKUP_ROOT)
    if not root.exists():
        return []
    # A named set, or something a person put there, is not a backup.
    found = [folder for folder in root.iterdir() if folder.is_dir() and _is_backup_name(folder.name)]
    return sorted(found, key=lambda path: path.name, reverse=True)


def latest_backup(root: PathInput | None = None, name: str = DEFAULT_NAME) -> Path | None:
    """The path of the most recent backup in the set or the old layout, or None."""
    candidates = [set_path(root, name) / folder for folder in list_backups(root, name)]
    candidates += legacy_backups(root)
    return max(candidates, key=lambda path: path.name) if candidates else None


def cleanup_old_backups(root: PathInput | None = None, name: str = DEFAULT_NAME, keep: int = RETAIN_BACKUPS) -> list[str]:
    """Delete all but the newest `keep` backups in a set. Returns the names removed.

    A folder that fails to delete is skipped, not raised.
    """
    base = set_path(root, name)
    if not base.exists():
        return []

    removed: list[str] = []
    for folder_name in list_backups(root, name)[keep:]:
        try:
            shutil.rmtree(base / folder_name)
            removed.append(folder_name)
        except OSError:
            pass
    return removed


def make_backup(database: PathInput | None, media: PathInput, root: PathInput | None = None, name: str = DEFAULT_NAME,
                keep: int = RETAIN_BACKUPS) -> BackupResult | None:
    """Copy the database and media folder into a new timestamped folder, then prune.

    The media copy is always called `MEDIA_FOLDER_NAME`, whatever `media` is called.
    `database` is None when there is no SQLite file to copy. Returns
    `{path, database_bytes, media_files, removed}`, or None if neither exists.
    """
    database = Path(database) if database is not None else None
    media = Path(media)
    if (database is None or not database.exists()) and not media.exists():
        return None

    destination = set_path(root, name) / datetime.now().strftime(BACKUP_FOLDER_FORMAT)
    destination.mkdir(parents=True, exist_ok=True)

    result: BackupResult = {'path': destination, 'database_bytes': 0, 'media_files': 0, 'removed': []}

    if database is not None and database.exists():
        copy_sqlite_database(database, destination / database.name)
        result['database_bytes'] = database.stat().st_size

    if media.exists():
        shutil.copytree(media, destination / MEDIA_FOLDER_NAME, dirs_exist_ok=True)
        result['media_files'] = sum(1 for path in media.rglob('*') if path.is_file())

    result['removed'] = cleanup_old_backups(root, name, keep)
    return result
