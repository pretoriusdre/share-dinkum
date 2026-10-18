"""Snapshot today's capital gains figures, per fiscal year with a sale or a trust statement.

Gains are recomputed on every report, so take a snapshot of lodged years before upgrading
or changing tax settings. `CGTBasisChangeReport` then shows what moved.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app import cgt
from share_dinkum_app.management.commands._portfolios import select_accounts
from share_dinkum_app.models import (
    Account, CGTReturnSnapshot, FiscalYear, LodgedSnapshotError)


class Command(BaseCommand):
    help = 'Record the capital gains figures for a fiscal year as they stand today.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument(
            '--fiscal-year',
            help='A single year, such as "FY2024/25". Omit for every year with a sale or a '
                 'trust statement.')
        parser.add_argument(
            '--lodged', action='store_true',
            help='Mark these as the figures actually lodged with the ATO. Use it when you '
                 'are capturing a year you have already filed.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args: Any, **options: Any) -> None:
        accounts = select_accounts(options['account'])

        for account in accounts:
            self._handle_account(account, options)

    def _handle_account(self, account: Account, options: dict[str, Any]) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(str(account)))

        years = self._years(account, options['fiscal_year'])
        if not years:
            self.stdout.write('  No sales or trust statements, so there are no capital gains to record.')
            return

        basis = cgt.residency_basis(account)
        if basis == cgt.BASIS_LEGACY:
            self.stdout.write(
                '  Residency is not declared, so these figures assume an Australian resident '
                'throughout and a flat 50% discount. They are recorded as such.')

        recorded = 0
        for fiscal_year in years:
            if options['dry_run']:
                self.stdout.write(f'  would capture {fiscal_year.name} on basis {basis}')
                recorded += 1
                continue

            try:
                snapshot = CGTReturnSnapshot.capture(
                    account=account,
                    fiscal_year=fiscal_year,
                    basis=basis,
                    is_lodged=options['lodged'],
                )
            except LodgedSnapshotError as exc:
                self.stdout.write(self.style.WARNING(f'  {exc}'))
                continue
            recorded += 1
            self.stdout.write(
                f'  {fiscal_year.name}: {len(snapshot.rows)} event(s), '
                f'net capital gain {snapshot.net_capital_gain}')

        verb = 'would record' if options['dry_run'] else 'recorded'
        self.stdout.write(self.style.SUCCESS(f'  {verb} {recorded} year(s).'))

    def _years(self, account: Account, wanted: str | None) -> list[FiscalYear]:
        """The named fiscal year, or every year with a sale or a trust statement."""
        if wanted:
            fiscal_year = FiscalYear.objects.filter(name=wanted).first()
            if fiscal_year is None:
                raise CommandError(
                    f'No fiscal year named "{wanted}". Names look like "FY2024/25".')
            return [fiscal_year]

        return cgt.years_with_events(account)
