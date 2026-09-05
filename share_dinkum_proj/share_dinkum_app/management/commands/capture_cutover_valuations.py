"""Record what each holding was worth on 30 June 2027, and on any other deemed sale date.

s112-155 deems every holding sold at market value just before 1 July 2027. Without a figure
for that day, a gain on a parcel held across the cutover cannot be split into the half that
keeps the 50% discount and the half that is indexed instead, so the whole thing falls back to
a single unsplit gain.

The value is needed for one specific day, and the ordinary price refresh will not supply it:
`update_price_history` runs on demand and only for instruments with an open position or a
recent sale, so an instrument sold in 2029 may have no price recorded for 2027 at all. Nor
can it be reconstructed later from a provider that has delisted the security.

Run this once, soon after the cutover, and then leave it. Existing valuations are never
overwritten unless `--overwrite` is given, because a user who had to source a value for an
unlisted holding should not have it replaced by a stale closing price.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from share_dinkum_app.cgt import cutover, residency
from share_dinkum_app.choices import ValuationSource
from share_dinkum_app.models import Account, Instrument, InstrumentValuation


class Command(BaseCommand):
    help = 'Record market values for the 1 July 2027 deemed sale and other reset dates.'

    def add_arguments(self, parser):
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument(
            '--date', help='A single date to value, as YYYY-MM-DD. Omit for every reset '
                           'date the account has, which includes any departure or arrival.')
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Replace values already recorded. Off by default, so a value the user '
                 'sourced themselves is not silently overwritten.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        accounts = Account.objects.all()
        if options['account']:
            accounts = accounts.filter(description=options['account'])
            if not accounts.exists():
                raise CommandError(f'No portfolio named "{options["account"]}".')

        for account in accounts:
            self._handle_account(account, options)

    def _handle_account(self, account, options):
        self.stdout.write(self.style.MIGRATE_HEADING(str(account)))

        if options['date']:
            targets = [(date.fromisoformat(options['date']), cutover.PURPOSE_CUTOVER)]
        else:
            targets = cutover.deemed_reset_dates(account)
            if not residency.periods(account):
                self.stdout.write(
                    '  Residency is not declared, so only the 1 July 2027 cutover is '
                    'valued. A departure or arrival would add its own reset date.')

        instruments = Instrument.objects.filter(account=account, is_active=True)
        recorded = skipped = missing = 0

        for valuation_date, purpose in targets:
            # The deemed sale happens just before the cutover, so it is the previous day
            # that has to be valued, not the day the new regime starts.
            day = (cutover.DEEMED_SALE_DATE
                   if purpose == cutover.PURPOSE_CUTOVER else valuation_date)

            for instrument in instruments:
                existing = InstrumentValuation.objects.filter(
                    account=account, instrument=instrument,
                    valuation_date=day, purpose=purpose).first()
                if existing and not options['overwrite']:
                    skipped += 1
                    continue

                value, source = cutover.unit_value_at(
                    instrument, day, purpose=purpose,
                    prefer_recorded=not options['overwrite'])
                if value is None:
                    missing += 1
                    self.stdout.write(self.style.WARNING(
                        f'  {instrument.name}: no price for {day.isoformat()}. Enter one by '
                        f'hand, or its gain cannot be split across the cutover.'))
                    continue

                if options['dry_run']:
                    self.stdout.write(
                        f'  {instrument.name}: {value} on {day.isoformat()} ({source})')
                    recorded += 1
                    continue

                InstrumentValuation.objects.update_or_create(
                    account=account, instrument=instrument,
                    valuation_date=day, purpose=purpose,
                    defaults={'unit_value': value,
                              'source': source or ValuationSource.USER},
                )
                recorded += 1

        verb = 'would record' if options['dry_run'] else 'recorded'
        self.stdout.write(self.style.SUCCESS(
            f'  {verb} {recorded}, kept {skipped} already recorded, {missing} with no price.'))
        if missing:
            self.stdout.write(self.style.WARNING(
                '  Holdings with no price need a value entered by hand before their gains '
                'can be reported for a sale after 1 July 2027.'))
