"""A test runner that keeps uploaded files out of the real media folder.

The test database is in memory and is rolled back, but a file saved by a test lands on disk.
Pointed at the real `MEDIA_ROOT`, every run left exports and contract notes there that no record
named, and those were then copied into every backup.
"""

import shutil
import tempfile
from typing import Any

from django.test import override_settings
from django.test.runner import DiscoverRunner


class TempMediaRunner(DiscoverRunner):
    """Runs the tests with `MEDIA_ROOT` set to a folder that is deleted afterwards."""

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        self._media_dir = tempfile.mkdtemp(prefix='share-dinkum-test-media-')
        self._media_override = override_settings(MEDIA_ROOT=self._media_dir)
        self._media_override.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        self._media_override.disable()
        shutil.rmtree(self._media_dir, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
