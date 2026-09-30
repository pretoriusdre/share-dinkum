"""Fetch every stored price again, as traded that day.

Prices used to be stored as the data provider adjusts them: lowered by every dividend paid
since, and divided by every split since. Nothing in the application wants that. A deemed
disposal needs what a unit was worth on the day, and the dashboard's value chart counts a
holding in that day's units. Prices are now stored as traded, but a stored day is only
replaced when it is fetched again, which is what this does. Until it has run, the value chart
is off for the days stored before.

Nothing is deleted. A delisted security the provider no longer has keeps the prices it has,
and is listed so you know those are still adjusted.

    uv run dev refetch_price_history --dry-run
    uv run dev refetch_price_history --account "Default Portfolio"
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Min

from share_dinkum_app.choices import ValuationSource
from share_dinkum_app.models import (
    Account, Buy, Instrument, InstrumentPriceHistory, InstrumentValuation)


class Command(BaseCommand):
    help = 'Fetch every stored price again, as traded rather than adjusted. Deletes nothing.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args: Any, **options: Any) -> None:
        accounts = Account.objects.all()
        if options['account']:
            accounts = accounts.filter(description=options['account'])
            if not accounts.exists():
                raise CommandError(f'No portfolio named "{options["account"]}".')

        for account in accounts:
            self._handle_account(account, options['dry_run'])

    def _handle_account(self, account: Account, dry_run: bool) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(str(account)))

        kept = []
        for instrument in Instrument.objects.filter(account=account, is_active=True).order_by('name'):
            stored = InstrumentPriceHistory.objects.filter(account=account, instrument=instrument)
            held = stored.count()
            start = (stored.aggregate(first=Min('date'))['first']
                     or Buy.objects.filter(account=account, instrument=instrument)
                     .aggregate(first=Min('date'))['first'])
            if start is None:
                continue

            if dry_run:
                self.stdout.write(
                    f'  {instrument.name}: would fetch from {start.isoformat()} '
                    f'({held} day(s) held)')
                continue

            fetched = instrument.update_price_history(start_date=start)
            if not fetched:
                if held:
                    kept.append(instrument.name)
                self.stdout.write(self.style.WARNING(
                    f'  {instrument.name}: nothing fetched; {held} day(s) kept as they were'))
                continue
            self.stdout.write(f'  {instrument.name}: {fetched} day(s) from {start.isoformat()}')

        if kept:
            self.stdout.write(self.style.WARNING(
                '  The provider had nothing for these, so their prices are still adjusted for '
                f'later dividends and splits: {", ".join(kept)}. Record a value by hand '
                'wherever one of them needs a market value.'))

        copied = InstrumentValuation.objects.filter(
            account=account, is_active=True, source=ValuationSource.PRICE_HISTORY,
        ).select_related('instrument').order_by('valuation_date')
        if copied:
            self.stdout.write(self.style.WARNING(
                '  These valuations were copied from a stored price, which may have been '
                'adjusted. Check each against the price it traded at, or capture it again with '
                '"capture_cutover_valuations --overwrite" (which also replaces any you entered):'))
            for valuation in copied:
                self.stdout.write(
                    f'    {valuation.instrument.name} on {valuation.valuation_date.isoformat()}: '
                    f'{valuation.unit_value}')
