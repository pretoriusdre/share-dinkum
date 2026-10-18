"""Write every figure for a portfolio to sorted CSVs, to diff before and after a change.

Take one dump, make the change, take another on the same day, and compare the folders. Nothing is
written to the database: the dump runs in a transaction that is rolled back, with market data
fetches stubbed out. See `figures_dump`.
"""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from share_dinkum_app import figures_dump
from share_dinkum_app.management.commands._portfolios import select_accounts


class Command(BaseCommand):
    help = 'Write every figure for a portfolio to sorted CSVs, to compare before and after a change.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--account', required=True, help='Portfolio name.')
        parser.add_argument('--out', required=True, help='Folder to write the CSVs to.')

    def handle(self, *args: Any, **options: Any) -> None:
        account = select_accounts(options['account']).get()

        tables = figures_dump.dump(account)
        written = figures_dump.write(tables, Path(options['out']))
        for path in written:
            self.stdout.write(f'  {path}  ({max(len(tables[path.stem]) - 1, 0)} rows)')
        self.stdout.write(self.style.SUCCESS(f'Wrote {len(written)} tables.'))
