"""The installed version, and whether GitHub has a newer release.

The version comes from package metadata (set from pyproject.toml by `uv sync`). A failed
update check logs a warning and reports no update, so it never breaks the page.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path
from typing import Any

import requests

from django.conf import settings

logger = logging.getLogger(__name__)


PACKAGE_NAME = 'share-dinkum'

# Used when the project has not been installed, eg a plain `git clone` that never had `uv sync` run.
# Keep this in step with the version in pyproject.toml.
FALLBACK_VERSION = '0.4.0'

RELEASES_API_URL = 'https://api.github.com/repos/pretoriusdre/share-dinkum/releases/latest'
RELEASES_PAGE_URL = 'https://github.com/pretoriusdre/share-dinkum/releases'

# Short enough that a slow or unreachable network is not noticeable on the page which triggers it.
REQUEST_TIMEOUT_SECONDS = 3
CHECK_INTERVAL = timedelta(hours=24)
# A check that could not reach GitHub is remembered for this long, so an offline machine is not
# made to wait on the request every time the dashboard loads.
FAILED_CHECK_RETRY = timedelta(hours=1)


def get_version() -> str:
    try:
        return installed_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


__version__ = get_version()


def get_cache_path() -> Path:
    """Path of the file caching the last update check."""
    return Path(settings.BASE_DIR) / '.update_check.json'


def parse_version(text: Any) -> tuple[int, ...] | None:
    """Turn 'v1.2.3' into (1, 2, 3), or None for anything which is not that shape."""
    if not text:
        return None
    parts = str(text).strip().lstrip('vV').split('.')
    try:
        return tuple(int(part) for part in parts[:3])
    except ValueError:
        return None


def read_cache() -> dict[str, Any] | None:
    """The cached check if still current, else None.

    Current for CHECK_INTERVAL after reaching GitHub, FAILED_CHECK_RETRY after failing to.
    """
    try:
        cached = json.loads(get_cache_path().read_text(encoding='utf-8'))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    interval = CHECK_INTERVAL if cached.get('reached', True) else FAILED_CHECK_RETRY
    if age > interval:
        return None
    return cached


def write_cache(latest_version: str | None, release_url: str | None, reached: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'latest_version': latest_version,
        'release_url': release_url,
        'reached': reached,
    }
    try:
        get_cache_path().write_text(json.dumps(payload, indent=2), encoding='utf-8')
    except OSError as e:
        # Not being able to remember the answer only costs an extra request next time.
        logger.warning(f'Could not save the update check result: {e}')
    return payload


def fetch_latest_release() -> tuple[bool, str | None, str | None]:
    """Return `(reached_github, tag_name, release_url)` for the latest GitHub release.

    A 404 (no releases yet) counts as reaching GitHub.
    """
    try:
        response = requests.get(
            RELEASES_API_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={'Accept': 'application/vnd.github+json'},
        )
        if response.status_code == 404:
            return True, None, None
        response.raise_for_status()
        release = response.json()
        return True, release.get('tag_name'), release.get('html_url')

    except Exception as e:
        logger.warning(f'Could not check for updates: {e}', exc_info=True)
        return False, None, None


def check_for_update(force: bool = False) -> dict[str, Any]:
    """A dict of current and latest version, release URL, and whether an update is available.

    Uses the cache unless `force`. If GitHub cannot be reached, the update fields stay empty.
    """
    result: dict[str, Any] = {
        'current_version': __version__,
        'latest_version': None,
        'release_url': RELEASES_PAGE_URL,
        'update_available': False,
    }

    cached = None if force else read_cache()
    if cached is None:
        reached_github, tag_name, release_url = fetch_latest_release()
        if not reached_github:
            write_cache(latest_version=None, release_url=None, reached=False)
            return result
        cached = write_cache(latest_version=tag_name, release_url=release_url)
    if not cached.get('reached', True):
        return result

    latest_version = cached.get('latest_version')
    result['latest_version'] = latest_version
    result['release_url'] = cached.get('release_url') or RELEASES_PAGE_URL

    latest = parse_version(latest_version)
    current = parse_version(__version__)
    if latest and current:
        result['update_available'] = latest > current

    return result
