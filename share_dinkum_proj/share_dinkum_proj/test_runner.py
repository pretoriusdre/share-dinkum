"""A test runner that keeps uploaded files out of the real media folder, and checks holdings.

The test database is in memory and is rolled back, but a file saved by a test lands on disk.
Pointed at the real `MEDIA_ROOT`, every run left exports and contract notes there that no record
named, and those were then copied into every backup.

After each test that uses the database, every portfolio's holding is replayed, and a replay that
would change it fails the test (`holdings.shadow`). So every scenario the tests build checks that
the signals and the replay agree. A test that damages a holding on purpose sets
`HOLDINGS_MAY_DIFFER = True`, on its class or its method.
"""

import shutil
import tempfile
from typing import Any

from django.test import TransactionTestCase, override_settings
from django.test.runner import DiscoverRunner


def _check_holdings(test: TransactionTestCase) -> None:
    method = getattr(test, test._testMethodName, None)
    if getattr(test, 'HOLDINGS_MAY_DIFFER', False) or getattr(method, 'HOLDINGS_MAY_DIFFER', False):
        return
    from django.db import connection
    if connection.needs_rollback:
        return  # The test broke its own transaction on purpose (an IntegrityError, say).
    from share_dinkum_app.holdings import shadow
    from share_dinkum_app.models import Account

    for account in Account.objects.all():
        shadow.check(account, test.id())


class TempMediaRunner(DiscoverRunner):
    """Runs the tests with `MEDIA_ROOT` set to a folder that is deleted afterwards."""

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        self._media_dir = tempfile.mkdtemp(prefix='share-dinkum-test-media-')
        self._overrides = override_settings(MEDIA_ROOT=self._media_dir, HOLDINGS_SHADOW='assert')
        self._overrides.enable()

        # A cleanup runs after tearDown and before the test's data is rolled back, and what it
        # raises is reported as the test's error.
        self._do_cleanups = TransactionTestCase.doCleanups

        def do_cleanups(test: TransactionTestCase) -> Any:
            if not getattr(test, '_holdings_checked', False):
                test._holdings_checked = True  # type: ignore[attr-defined]
                test.addCleanup(_check_holdings, test)
            return self._do_cleanups(test)

        TransactionTestCase.doCleanups = do_cleanups  # type: ignore[method-assign,assignment]

    def teardown_test_environment(self, **kwargs: Any) -> None:
        TransactionTestCase.doCleanups = self._do_cleanups  # type: ignore[method-assign]
        self._overrides.disable()
        shutil.rmtree(self._media_dir, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
