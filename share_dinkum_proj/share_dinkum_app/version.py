"""The installed version, and whether a newer one has been released.

The version itself comes from the installed package metadata, which `uv sync` writes from
pyproject.toml, so the version is set in exactly one place.

The update check asks GitHub for the latest release. It is deliberately incapable of breaking the
page it appears on: every failure path returns "no update known" and logs a warning, so being
offline, rate limited, or ahead of the first published release all look the same to the caller.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path

import requests

from django.conf import settings

logger = logging.getLogger(__name__)


PACKAGE_NAME = 'share-dinkum'

# Used when the project has not been installed, eg a plain `git clone` that never had `uv sync` run.
# Keep this in step with the version in pyproject.toml.
FALLBACK_VERSION = '0.2.0'

RELEASES_API_URL = 'https://api.github.com/repos/pretoriusdre/share-dinkum/releases/latest'
RELEASES_PAGE_URL = 'https://github.com/pretoriusdre/share-dinkum/releases'

# Short enough that a slow or unreachable network is not noticeable on the page which triggers it.
REQUEST_TIMEOUT_SECONDS = 3
CHECK_INTERVAL = timedelta(hours=24)


def get_version():
    try:
        return installed_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


__version__ = get_version()


def get_cache_path():
    """Where the last check is remembered, so the dashboard is not calling out on every page load."""
    return Path(settings.BASE_DIR) / '.update_check.json'


def parse_version(text):
    """Turn 'v1.2.3' into (1, 2, 3), or None for anything which is not that shape."""
    if not text:
        return None
    parts = str(text).strip().lstrip('vV').split('.')
    try:
        return tuple(int(part) for part in parts[:3])
    except ValueError:
        return None


def read_cache():
    """The last check, if it is still recent enough to reuse. None means go and look again."""
    try:
        cached = json.loads(get_cache_path().read_text(encoding='utf-8'))
        checked_at = datetime.fromisoformat(cached['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    if datetime.now(timezone.utc) - checked_at > CHECK_INTERVAL:
        return None
    return cached


def write_cache(latest_version, release_url):
    payload = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'latest_version': latest_version,
        'release_url': release_url,
    }
    try:
        get_cache_path().write_text(json.dumps(payload, indent=2), encoding='utf-8')
    except OSError as e:
        # Not being able to remember the answer only costs an extra request next time.
        logger.warning(f'Could not save the update check result: {e}')
    return payload


def fetch_latest_release():
    """Ask GitHub for the latest release.

    Returns (reached_github, tag_name, release_url). A 404 counts as reaching GitHub: it means no
    release has been published yet, which is a real answer worth remembering rather than a failure.
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


def check_for_update(force=False):
    """The installed version, and the newer one if there is one.

    The result is always safe to render. Where the check could not be made, or no release has been
    published, the update fields are simply empty.
    """
    result = {
        'current_version': __version__,
        'latest_version': None,
        'release_url': RELEASES_PAGE_URL,
        'update_available': False,
    }

    cached = None if force else read_cache()
    if cached is None:
        reached_github, tag_name, release_url = fetch_latest_release()
        if not reached_github:
            return result
        cached = write_cache(latest_version=tag_name, release_url=release_url)

    latest_version = cached.get('latest_version')
    result['latest_version'] = latest_version
    result['release_url'] = cached.get('release_url') or RELEASES_PAGE_URL

    latest = parse_version(latest_version)
    current = parse_version(__version__)
    if latest and current:
        result['update_available'] = latest > current

    return result
