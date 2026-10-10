"""Work each portfolio's holding out again from its trades, and show where it differs from what is stored.

Parcels, sale allocations and adjustment spreads are stored as they were worked out when each
record was entered. This works them out again in date order, keeping every decision already made
(which parcels a sale used, how an adjustment was divided between buys), and lists any figure that
differs. It writes nothing.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app.holdings import compare
from share_dinkum_app.models import Account, Instrument


class Command(BaseCommand):
    help = 'Show where the stored holdings differ from the holdings worked out again from the trades.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument('--respread', action='store_true',
                            help='Also list adjustments a fresh spread would divide differently between buys.')
        parser.add_argument('--rebuild', action='store_true',
                            help='Rewrite each differing holding as worked out from its trades, accepting the '
                                 'differences listed. Decisions already made are kept.')

    def handle(self, *args: Any, **options: Any) -> None:
        accounts = Account.objects.all()
        if options['account']:
            accounts = accounts.filter(description=options['account'])
            if not accounts.exists():
                raise CommandError(f'No portfolio named "{options["account"]}".')

        for account in accounts:
            self.stdout.write(self.style.MIGRATE_HEADING(str(account)))
            report = compare.check(account)
            self.stdout.write(
                f'  {report.parcels_stored} parcels stored, {report.parcels_replayed} replayed. '
                f'{report.shape_differences} differ only in shape, {report.rounding} adjustment(s) '
                f'by under a cent.')
            for problem in report.problems:
                self.stdout.write(self.style.ERROR(f'  {problem}'))
            for difference in report.differences:
                self.stdout.write(self.style.WARNING(f'  {difference}'))
            if report.respread:
                if options['respread']:
                    for line in report.respread:
                        self.stdout.write(self.style.NOTICE(f'  {line}'))
                else:
                    self.stdout.write(
                        f'  {len(report.respread)} adjustment part(s) would be divided differently if spread '
                        f'again. They are kept as entered; --respread lists them.')
            if report.count:
                self.stdout.write(self.style.WARNING(f'  {report.count} difference(s).'))
            else:
                self.stdout.write(self.style.SUCCESS('  The stored holding agrees with the replay.'))
            if options['rebuild']:
                self._rebuild(account)

    def _rebuild(self, account: Account) -> None:
        from django.db import transaction

        from share_dinkum_app.holdings import apply, live

        for instrument in Instrument.objects.filter(account=account).order_by('name'):
            with transaction.atomic():
                result = apply.rebuild(account, instrument)
                Instrument.objects.filter(pk=instrument.pk).update(holdings_differ=False)
            if not result.empty:
                self.stdout.write(self.style.SUCCESS(f'  {instrument.name}: rebuilt ({result.summary()}).'))
        if not live.active():
            self.stdout.write('  The creation signals still write holdings (HOLDINGS_WRITER=signals).')
