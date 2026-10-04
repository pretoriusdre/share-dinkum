"""Repair what bugs fixed in 0.3.0 left behind, and list what needs a person.

Repaired:

* Stored figures (a parcel's cost base and sold flag, an instrument's holding) are
  recalculated from the live ones. Reports always read the live figures, so no capital gain
  changes from this; only the copies shown in the admin lists and exports do.
* Cost base adjustments left on a parcel a share split replaced are carried to the parcels
  that replaced it. Until then they are missing from the cost base, so this does change gains.
* Exchange rates standing in for one that could not be fetched are fetched again.

Listed, because only the person who entered them knows the answer: sales with units
allocated to no parcel, parcels allocated for more than they hold, parcels whose cost base
has gone below zero, and cost base adjustments spread unconverted or at a stand-in rate
(delete each and enter it again).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from share_dinkum_app import data_checks, recalculate
from share_dinkum_app.models import Account, CostBaseAdjustment


class Command(BaseCommand):
    help = 'Recalculate stored figures, reattach split adjustments, refetch stand-in rates, and list what needs fixing by hand.'

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

        if dry_run:
            findings = data_checks.run(account)
            if not findings:
                self.stdout.write('  Nothing found. A repair would still recalculate every stored figure.')
            for finding in findings:
                action = 'would repair' if finding.repairable else 'needs fixing by hand'
                self.stdout.write(f'  {finding.summary} ({action}).')
            self._list_for_a_person(account)
            return

        with transaction.atomic():
            reattached = recalculate.reattach_adjustments(account)
            refetched = recalculate.refetch_placeholder_rates(account)
            recalculated = recalculate.account(account)

        if reattached:
            self.stdout.write(self.style.SUCCESS(
                f'  carried {reattached} cost base adjustment allocation(s) to the parcels '
                f'that replaced theirs.'))
        if refetched:
            self.stdout.write(self.style.SUCCESS(f'  fetched {refetched} exchange rate(s).'))
        self.stdout.write(self.style.SUCCESS(
            f'  recalculated the stored figures of {recalculated} record(s).'))

        remaining = data_checks.run(account)
        for finding in remaining:
            style = self.style.WARNING if finding.affects_gains else self.style.NOTICE
            self.stdout.write(style(f'  {finding.summary}.'))
        self._list_for_a_person(account)

    def _list_for_a_person(self, account: Account) -> None:
        sales = list(data_checks.unallocated_sales(account))
        if sales:
            self.stdout.write(self.style.WARNING(
                '  Sales with units allocated to no parcel. Add sell allocations for them, or '
                'check the sale is not larger than the holding or dated before its purchase:'))
            for sale in sales:
                self.stdout.write(
                    f'    {sale.instrument.name} on {sale.date}: '
                    f'{(sale.quantity - sale.allocated).normalize():f} of '
                    f'{sale.quantity.normalize():f} units')

        oversold = list(data_checks.oversold_parcels(account))
        if oversold:
            self.stdout.write(self.style.WARNING(
                '  Parcels allocated for more units than they hold. Delete the extra sell '
                'allocations:'))
            for parcel in oversold:
                self.stdout.write(
                    f'    {parcel.buy.instrument.name} bought {parcel.buy.date}: '
                    f'{parcel.sold.normalize():f} sold of {parcel.parcel_quantity.normalize():f}')

        negative = list(data_checks.negative_cost_base_parcels(account))
        if negative:
            self.stdout.write(self.style.WARNING(
                '  Parcels whose cost base is below zero. The excess is a capital gain in the '
                'year the decrease took it there (CGT event E10); report it for that year:'))
            for parcel in negative:
                self.stdout.write(
                    f'    {parcel.buy.instrument.name} bought {parcel.buy.date}: '
                    f'{parcel.calculated_total_cost_base}')

        adjustments = (
            list(CostBaseAdjustment.with_unconverted_allocations(account))
            + list(data_checks.unbalanced_adjustments(account)))
        if adjustments:
            self.stdout.write(self.style.WARNING(
                f'  Cost base adjustments allocated wrongly (unconverted to {account.currency}, '
                f'or at a stand-in rate), so the cost base of the parcels they reached is wrong. '
                f'Delete each one and enter it again:'))
            for adjustment in {adjustment.pk: adjustment for adjustment in adjustments}.values():
                self.stdout.write(f'    {adjustment}')

        empty = list(data_checks.empty_adjustments(account))
        if empty:
            self.stdout.write(self.style.WARNING(
                '  Cost base adjustments for a year when none of the instrument was held, so '
                'they reached no parcel. Check the year and instrument, then delete each one '
                'and enter it again:'))
            for adjustment in empty:
                self.stdout.write(f'    {adjustment}')
