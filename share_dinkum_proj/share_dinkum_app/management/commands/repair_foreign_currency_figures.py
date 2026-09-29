"""Repair figures left unconverted by a bug fixed in 0.3.0.

Before 0.3.0 a foreign-currency trade got its exchange rate only after the records built from
it. A buy's parcel therefore stored its cost base in the trade's currency, and a cost base
adjustment was allocated to parcels without being converted.

Parcels are recalculated. Reports always read the live figures, so no capital gain changes;
only the stored copy shown in the parcel list and in exports does. Adjustments are listed
but not changed: re-allocating one spreads it over the parcels again, so that is left to the
operator, who deletes the adjustment and enters it again.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from share_dinkum_app.models import Account, CostBaseAdjustment, Parcel


class Command(BaseCommand):
    help = 'Recalculate parcel cost bases stored in a foreign currency, and list adjustments that need re-entering.'

    def add_arguments(self, parser):
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        accounts = Account.objects.all()
        if options['account']:
            accounts = accounts.filter(description=options['account'])
            if not accounts.exists():
                raise CommandError(f'No portfolio named "{options["account"]}".')

        for account in accounts:
            self._handle_account(account, options['dry_run'])

    def _handle_account(self, account, dry_run):
        self.stdout.write(self.style.MIGRATE_HEADING(str(account)))
        currency = str(account.currency)

        parcels = list(Parcel.with_unconverted_cost_base(account).select_related('buy__instrument'))
        if not parcels:
            self.stdout.write(f'  Every parcel cost base is stored in {currency}.')
        elif dry_run:
            self.stdout.write(f'  would recalculate {len(parcels)} parcel(s):')
            for parcel in parcels:
                self.stdout.write(f'    {parcel.description}: {parcel.calculated_total_cost_base}')
        else:
            with transaction.atomic():
                for parcel in parcels:
                    parcel.save()
            remaining = Parcel.with_unconverted_cost_base(account).count()
            self.stdout.write(self.style.SUCCESS(
                f'  recalculated {len(parcels) - remaining} parcel(s) in {currency}.'))
            if remaining:
                self.stdout.write(self.style.WARNING(
                    f'  {remaining} parcel(s) are still not in {currency}. Check that their '
                    f'buys have an exchange rate.'))

        adjustments = list(CostBaseAdjustment.with_unconverted_allocations(account))
        if adjustments:
            self.stdout.write(self.style.WARNING(
                f'  {len(adjustments)} cost base adjustment(s) were allocated without being '
                f'converted to {currency}, so the cost base of the parcels they reached is '
                f'wrong. Delete each one and enter it again; it is then allocated in {currency}:'))
            for adjustment in adjustments:
                self.stdout.write(f'    {adjustment}')
