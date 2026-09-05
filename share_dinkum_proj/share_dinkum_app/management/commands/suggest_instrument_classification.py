"""Suggest a legal form for instruments that have not been classified.

Capital gains reporting needs to know whether each holding is a company or a trust, because
the schedule reports them in different boxes and a ticker does not say which it is. This
command fills in what it can and lists what it cannot, so the remaining work is a short,
visible list rather than a silent gap.

Nothing here overwrites an answer the user has given. Suggestions are recorded as
suggestions, and a capital gains schedule built on unconfirmed classifications reports
itself as a draft.

    uv run dev suggest_instrument_classification
    uv run dev suggest_instrument_classification --account "Default Portfolio"
    uv run dev suggest_instrument_classification --use-market-data
"""

from django.core.management.base import BaseCommand, CommandError

from share_dinkum_app import cgt, yfinanceinterface
from share_dinkum_app.choices import LegalForm, LegalFormSource
from share_dinkum_app.models import Account, Instrument, Market


class Command(BaseCommand):
    help = 'Suggest a legal form for unclassified instruments, and report what remains.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--account', type=str, default=None,
            help='Portfolio description. Defaults to every portfolio.')
        parser.add_argument(
            '--use-market-data', action='store_true',
            help='Also consult the price data provider. Slower, needs a network, and its '
                 'answer is weak: it reports stapled securities and property trusts as '
                 'ordinary shares, which is the distinction this command exists to make.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.')

    def handle(self, *args, **options):
        accounts = Account.objects.all()
        if options['account']:
            accounts = accounts.filter(description=options['account'])
            if not accounts.exists():
                raise CommandError(f"No portfolio named {options['account']!r}.")

        dry_run = options['dry_run']
        total_markets = total_instruments = 0
        unresolved = []

        for account in accounts:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{account.description}'))

            for market in Market.objects.filter(account=account, country__isnull=True):
                suggestion = cgt.suggested_country(market)
                if not suggestion:
                    self.stdout.write(f'  market {market.code}: country unknown, please set it')
                    continue
                if not dry_run:
                    market.country = suggestion
                    market.save()
                total_markets += 1
                self.stdout.write(f'  market {market.code}: country -> {suggestion}')

            instruments = Instrument.objects.filter(account=account).select_related('market')
            for instrument in instruments:
                if instrument.legal_form_source == LegalFormSource.USER:
                    continue
                if instrument.legal_form != LegalForm.UNKNOWN:
                    continue

                market_data = None
                if options['use_market_data']:
                    market_data = self._market_data(instrument)

                # What the holding has actually paid is better evidence than any list of
                # codes, so it is tried first.
                suggestion = cgt.suggest_legal_form_from_activity(instrument)
                basis = 'income history'
                if not suggestion:
                    suggestion = cgt.suggest_legal_form(instrument, market_data=market_data)
                    basis = 'known code'

                if not suggestion:
                    unresolved.append((account.description, instrument.name))
                    continue

                # Applied in memory either way, so a dry run reports the category the real
                # run would produce rather than the one it has now.
                instrument.legal_form = suggestion
                instrument.legal_form_source = LegalFormSource.SUGGESTED
                if not dry_run:
                    instrument.save()
                total_instruments += 1
                self.stdout.write(
                    f'  {instrument.name}: {suggestion} ({basis}) '
                    f'-> {cgt.asset_category(instrument)}')

        self.stdout.write('')
        verb = 'would set' if dry_run else 'set'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_markets} market countries and {total_instruments} legal forms.'))

        if unresolved:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'{len(unresolved)} instruments still need classifying. Until they are, a '
                f'capital gains schedule covering them can only be a draft:'))
            for account_name, instrument_name in unresolved:
                self.stdout.write(f'  {account_name}: {instrument_name}')
            self.stdout.write(
                '\nSet each one in the admin. The legal form is on the product disclosure '
                'statement or annual tax statement: a company issues shares, a trust issues '
                'units, and most ETFs are trusts.')

    def _market_data(self, instrument):
        """Provider metadata, or None. A lookup failure must not stop the command."""
        try:
            ticker = yfinanceinterface.yf.Ticker(instrument.yfinance_ticker_code)
            return ticker.info or None
        except Exception as error:                                      # noqa: BLE001
            self.stderr.write(f'  {instrument.name}: could not read market data ({error})')
            return None
