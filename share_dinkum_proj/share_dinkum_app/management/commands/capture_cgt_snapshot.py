"""Record what the capital gains figures are today, so a later change can be explained.

Capital gains are worked out on demand and never stored, which is what makes it safe to
correct a calculation: nothing already in the database has to be rewritten. The cost is that
improving a calculation silently changes what the application reports for a year you have
already lodged, and there is then no record of what it used to say.

A snapshot is that record. Take one for every year you have lodged, before upgrading or
before changing anything about how your gains are worked out -- declaring residency, setting
your taxpayer type, classifying an instrument. `CGTBasisChangeReport` then compares the
snapshot against a fresh calculation and tells you which lines moved and by how much.

This is the only way to create one. The figures come from the report rather than from
anything you type, and the model deliberately will not let you edit them afterwards: a
snapshot you can adjust is not evidence of anything.
"""

from django.core.management.base import BaseCommand, CommandError

from share_dinkum_app import cgt
from share_dinkum_app.models import Account, CGTReturnSnapshot, FiscalYear, Sell


class Command(BaseCommand):
    help = 'Record the capital gains figures for a fiscal year as they stand today.'

    def add_arguments(self, parser):
        parser.add_argument('--account', help='Portfolio name. Omit for every portfolio.')
        parser.add_argument(
            '--fiscal-year',
            help='A single year, such as "FY2024/25". Omit for every year that has a sale.')
        parser.add_argument(
            '--lodged', action='store_true',
            help='Mark these as the figures actually lodged with the ATO. Use it when you '
                 'are capturing a year you have already filed.')
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

        years = self._years(account, options['fiscal_year'])
        if not years:
            self.stdout.write('  No sales, so there are no capital gains to record.')
            return

        basis = cgt.residency_basis(account)
        if basis == cgt.BASIS_LEGACY:
            self.stdout.write(
                '  Residency is not declared, so these figures assume an Australian resident '
                'throughout and a flat 50% discount. They are recorded as such.')

        for fiscal_year in years:
            if options['dry_run']:
                self.stdout.write(f'  would capture {fiscal_year.name} on basis {basis}')
                continue

            snapshot = CGTReturnSnapshot.capture(
                account=account,
                fiscal_year=fiscal_year,
                basis=basis,
                is_lodged=options['lodged'],
            )
            self.stdout.write(
                f'  {fiscal_year.name}: {len(snapshot.rows)} disposal(s), '
                f'net {snapshot.totals.get("total_capital_gain")}')

        verb = 'would record' if options['dry_run'] else 'recorded'
        self.stdout.write(self.style.SUCCESS(f'  {verb} {len(years)} year(s).'))

    def _years(self, account, wanted):
        """Fiscal years worth snapshotting: the ones with a disposal in them."""
        if wanted:
            fiscal_year = FiscalYear.objects.filter(name=wanted).first()
            if fiscal_year is None:
                raise CommandError(
                    f'No fiscal year named "{wanted}". Names look like "FY2024/25".')
            return [fiscal_year]

        year_ids = (
            Sell.objects.filter(account=account, is_active=True)
            .values_list('calculated_fiscal_year', flat=True)
            .distinct()
        )
        return list(
            FiscalYear.objects.filter(id__in=[y for y in year_ids if y])
            .order_by('start_year')
        )
