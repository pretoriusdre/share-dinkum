"""The admin dashboard: what it shows, and the actions it offers.

Each action is one entry in `DASHBOARD_ACTIONS`, which supplies its URL, button, group,
`description` (what it does) and `status` (when it was last done, e.g. "Never exported.").
`ShareDinkumAdminSite` and the template both read from it.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
import logging
import tempfile

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Max
from django.http import FileResponse, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
# Dates used to reach the template as objects and be rendered by it. Now that the
# status line is built here it has to localise them itself, or a date that read
# "Aug. 28, 2026" on the page would silently become an ISO string.
from django.utils.formats import localize
from django.views.decorators.http import require_POST

from djmoney.money import Money

from share_dinkum_app import cgt, data_checks, version
from share_dinkum_app.choices import TaxpayerType
from share_dinkum_app.models import (
    Account,
    Buy,
    CGTReturnSnapshot,
    CurrentExchangeRate,
    DataExport,
    Distribution,
    Dividend,
    ExchangeRate,
    FiscalYear,
    Instrument,
    InstrumentPriceHistory,
    LodgedSnapshotError,
    ResidencyPeriod,
    Sell,
    ShareSplit,
)

logger = logging.getLogger(__name__)


def _select_account_for_user(user: Any) -> Account | None:
    if not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'visible_account', None)


def _decimal_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if not isinstance(value, Decimal):
        value = Decimal(value)
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _format_money(money: Any) -> str:
    if money is None:
        return ''
    amount = getattr(money, 'amount', None)
    if amount is None:
        return ''
    if not isinstance(amount, Decimal):
        amount = Decimal(amount)
    formatted_amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    currency = getattr(money, 'currency', '')
    currency_text = str(currency) if currency else ''
    if currency_text:
        return f"{currency_text} {formatted_amount:,.2f}"
    return f"{formatted_amount:,.2f}"


def _export_last_taken(account: Account | None) -> date | None:
    """The date of this portfolio's latest data export, or None."""
    if account is None:
        return None
    latest = (
        DataExport.objects
        .filter(account=account, is_active=True)
        .order_by('-created_at')
        .first()
    )
    return latest.created_at.date() if latest else None


def _snapshot_last_taken(account: Account | None) -> date | None:
    """The date of this portfolio's latest capital gains snapshot, or None."""
    if account is None:
        return None
    return (
        CGTReturnSnapshot.objects
        .filter(account=account, is_active=True)
        .aggregate(latest=Max('taken_at'))['latest']
    )


def _prices_last_updated(account: Account | None) -> date | None:
    """The latest date this portfolio has a price for, or None."""
    if account is None:
        return None
    return (
        InstrumentPriceHistory.objects
        .filter(account=account)
        .aggregate(latest=Max('date'))['latest']
    )


def _tax_settings_warning(account: Account | None) -> str | None:
    """A warning if capital gains rest on undeclared tax settings, else None.

    Raised when the account has sales and its taxpayer type or residency is undeclared, or
    its residency history has holes. Silenced by `tax_settings_reviewed_at`.
    """
    if account is None or account.tax_settings_reviewed_at is not None:
        return None
    if not Sell.objects.filter(account=account, is_active=True).exists():
        return None

    missing: list[str] = []
    if account.taxpayer_type == TaxpayerType.UNDECLARED:
        missing.append('who the portfolio belongs to')
    if not ResidencyPeriod.objects.filter(account=account, is_active=True).exists():
        missing.append('where you were tax resident')

    if not missing:
        problems = cgt.residency.coverage_problems(account)
        if not problems:
            return None
        return (
            f'Residency history for {account.description} has holes, so some gains cannot '
            f'be characterised: ' + ' '.join(problems)
        )
    return (
        f'Capital gains for {account.description} assume an Australian resident individual '
        f'holding throughout, and a flat 50% discount. You have not told it '
        f'{" or ".join(missing)}. If that assumption is right, saying so changes no figure; '
        f'if it is wrong, every discounted gain is wrong. Set it on the account, and add '
        f'your residency periods.'
    )


def _data_check_warning(account: Account | None) -> str | None:
    """A warning listing what `data_checks` found in the portfolio, else None.

    Cleared by the `repair_portfolio_data` command, or, for what needs a person, by fixing
    the records it lists.
    """
    if account is None:
        return None
    findings = data_checks.run(account)
    if not findings:
        return None

    affects_gains = any(finding.affects_gains for finding in findings)
    consequence = (
        'Some capital gains are wrong until these are dealt with.' if affects_gains else
        'Capital gains and reports are not affected, only the stored figures shown in the '
        'admin lists and exports.')
    return (
        f'{account.description} has '
        f'{"; ".join(finding.summary for finding in findings)}. {consequence} Run '
        f'`{data_checks.COMMAND}` to repair what it can and list the rest.'
    )


def prepare_dashboard_context(request: HttpRequest, context: dict[str, Any]) -> dict[str, Any]:
    account = _select_account_for_user(request.user)

    dashboard_message: str | None = None
    dashboard_message_level = 'info'
    total_portfolio_value_display: str | None = None
    parcel_labels: list[str] = []
    parcel_values: list[float] = []
    #: Summed from the chart's slices, not read off the account, so the caption always
    #: matches the chart (an instrument that fails to convert is left out of both).
    parcel_total = Decimal('0')
    parcel_total_display: str | None = None
    income_labels: list[str] = []
    dividend_series: list[float] = []
    distribution_series: list[float] = []
    area_chart_labels: list[str] = []
    area_chart_datasets: list[dict[str, Any]] = []
    value_chart_labels: list[str] = []
    value_chart_datasets: list[dict[str, Any]] = []
    dashboard_currency: str | None = None


    if not account:
        dashboard_message = (
            'No default account is associated with your user. '
            'Select a default account on your user profile or create an account to view dashboard insights.'
        )
        dashboard_message_level = 'warning'
    else:
        dashboard_currency = str(account.currency)
        instruments = list(
            Instrument.objects.filter(account=account, is_active=True)
            .select_related('market')
            .order_by('name')
        )

        for instrument in instruments:
            try:
                converted_value = instrument.value_held_converted
            except Exception as exc:  # pragma: no cover - defensive log
                logger.warning(
                    'Skipping instrument %s for dashboard value calculation: %s',
                    instrument,
                    exc,
                    exc_info=True,
                )
                continue

            if not converted_value:
                continue

            amount = getattr(converted_value, 'amount', None)
            if amount is None or amount <= 0:
                continue

            parcel_labels.append(instrument.name)
            parcel_values.append(_decimal_to_float(amount))
            parcel_total += amount

        if parcel_labels:
            parcel_total_display = _format_money(Money(parcel_total, account.currency))

        income_by_year: dict[int, dict[str, Any]] = {}

        dividends = Dividend.objects.filter(account=account, is_active=True)
        for dividend in dividends:
            fiscal_year = dividend.fiscal_year
            if not fiscal_year:
                continue

            label = fiscal_year.name or fiscal_year.get_name()
            year_key = fiscal_year.start_year
            entry = income_by_year.setdefault(
                year_key,
                {'label': label, 'dividends': Decimal('0'), 'distributions': Decimal('0')},
            )

            total_money = dividend.total_dividend_converted or dividend.total_dividend
            if total_money:
                entry['dividends'] += Decimal(total_money.amount)

        distributions = Distribution.objects.filter(account=account, is_active=True)
        for distribution in distributions:
            fiscal_year = distribution.fiscal_year
            if not fiscal_year:
                continue

            label = fiscal_year.name or fiscal_year.get_name()
            year_key = fiscal_year.start_year
            entry = income_by_year.setdefault(
                year_key,
                {'label': label, 'dividends': Decimal('0'), 'distributions': Decimal('0')},
            )

            total_money = distribution.total_distribution_converted or distribution.total_distribution
            if total_money:
                entry['distributions'] += Decimal(total_money.amount)

        sorted_years = sorted(income_by_year)
        for year in sorted_years:
            entry = income_by_year[year]
            income_labels.append(entry['label'])
            dividend_series.append(_decimal_to_float(entry['dividends']))
            distribution_series.append(_decimal_to_float(entry['distributions']))

        total_portfolio_value = account.portfolio_value_converted
        if total_portfolio_value is not None:
            total_portfolio_value_display = _format_money(total_portfolio_value)

        buy_records = list(
            Buy.objects.filter(
                account=account,
                is_active=True,
            ).values('instrument_id', 'date', 'quantity')
        )

        sell_records = list(
            Sell.objects.filter(
                account=account,
                is_active=True,
            ).values('instrument_id', 'date', 'quantity')
        )

        area_instrument_ids = sorted(
            {
                record['instrument_id']
                for record in buy_records + sell_records
            }
        )

        if area_instrument_ids:
            trade_adjustments: defaultdict[date, dict[Any, Decimal]] = defaultdict(dict)

            for record in buy_records:
                inst_id = record['instrument_id']
                if inst_id not in area_instrument_ids:
                    continue
                trade_adjustments[record['date']].setdefault(inst_id, Decimal('0'))
                trade_adjustments[record['date']][inst_id] += Decimal(record['quantity'])

            for record in sell_records:
                inst_id = record['instrument_id']
                if inst_id not in area_instrument_ids:
                    continue
                trade_adjustments[record['date']].setdefault(inst_id, Decimal('0'))
                trade_adjustments[record['date']][inst_id] -= Decimal(record['quantity'])

            # Holdings are in the units of each day, as the prices are, so a split is a step
            # on its ex-date. Without it a holding stayed in pre-split units for good and went
            # negative once the post-split units were sold.
            split_ratios: defaultdict[date, dict[Any, Decimal]] = defaultdict(dict)
            for split_record in ShareSplit.objects.filter(
                    account=account, is_active=True,
                    instrument_id__in=area_instrument_ids,
            ).values('instrument_id', 'date', 'quantity_before', 'quantity_after'):
                if split_record['quantity_before']:
                    ratios = split_ratios[split_record['date']]
                    ratios[split_record['instrument_id']] = ratios.get(
                        split_record['instrument_id'], Decimal('1')) * (
                        Decimal(split_record['quantity_after']) / Decimal(split_record['quantity_before']))

            available_dates = set(trade_adjustments.keys())

            if available_dates:
                earliest_date = min(available_dates)
                if earliest_date > date.min:
                    baseline_date = earliest_date - timedelta(days=1)
                    available_dates.add(baseline_date)
                available_dates.add(date.today())

                start_date = min(available_dates)
                end_date = max(available_dates)
                total_days = (end_date - start_date).days
                sorted_dates = [
                    start_date + timedelta(days=offset)
                    for offset in range(total_days + 1)
                ]


                instrument_by_id = {instrument.id: instrument for instrument in instruments}
                instrument_name_by_id = {inst_id: instrument.name for inst_id, instrument in instrument_by_id.items()}

                missing_instrument_ids = [
                    inst_id
                    for inst_id in area_instrument_ids
                    if inst_id not in instrument_name_by_id
                ]
                if missing_instrument_ids:
                    for instrument_obj in Instrument.objects.filter(account=account, id__in=missing_instrument_ids):
                        instrument_by_id[instrument_obj.id] = instrument_obj
                        instrument_name_by_id[instrument_obj.id] = instrument_obj.name


                ordered_instrument_ids = sorted(
                    area_instrument_ids,
                    key=lambda inst_id: instrument_name_by_id.get(inst_id, ''),
                )

                quantities_current = {inst_id: Decimal('0') for inst_id in ordered_instrument_ids}
                dataset_values: dict[Any, list[float]] = {inst_id: [] for inst_id in ordered_instrument_ids}
                quantities_by_date: dict[date, dict[Any, Decimal]] = {}

                for date_key in sorted_dates:
                    # The split first: it reaches what was held before its ex-date, and the
                    # day's own trades are already in post-split units.
                    for inst_id, ratio in split_ratios.get(date_key, {}).items():
                        quantities_current[inst_id] = (
                            quantities_current[inst_id] * ratio
                        ).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

                    adjustments = trade_adjustments.get(date_key, {})
                    for inst_id, delta in adjustments.items():
                        quantities_current[inst_id] = (
                            quantities_current[inst_id] + delta
                        ).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

                    quantities_by_date[date_key] = {
                        inst_id: quantities_current[inst_id]
                        for inst_id in ordered_instrument_ids
                    }

                    for inst_id in ordered_instrument_ids:
                        dataset_values[inst_id].append(float(quantities_current[inst_id]))


                area_chart_labels = [d.isoformat() for d in sorted_dates]
                area_chart_datasets = [
                    {
                        'label': instrument_name_by_id.get(inst_id, str(inst_id)),
                        'data': dataset_values[inst_id],
                    }
                    for inst_id in ordered_instrument_ids
                ]

                if sorted_dates:
                    start_date = sorted_dates[0]
                    end_date = sorted_dates[-1]

                    price_history_map: defaultdict[Any, dict[date, Any]] = defaultdict(dict)
                    price_history_qs = (
                        InstrumentPriceHistory.objects.filter(
                            account=account,
                            instrument_id__in=ordered_instrument_ids,
                            date__range=(start_date, end_date),
                        )
                        .values('instrument_id', 'date', 'close')
                    )
                    for price_record in price_history_qs:
                        price_history_map[price_record['instrument_id']][price_record['date']] = price_record['close']

                    initial_prices: dict[Any, Any] = {}
                    for inst_id in ordered_instrument_ids:
                        prior_close = (
                            InstrumentPriceHistory.objects.filter(
                                account=account,
                                instrument_id=inst_id,
                                date__lt=start_date,
                            )
                            .order_by('-date')
                            .values_list('close', flat=True)
                            .first()
                        )
                        if prior_close is not None:
                            initial_prices[inst_id] = prior_close

                    account_currency = str(account.currency)
                    instrument_currency_by_id: dict[Any, str] = {}
                    currencies_requiring_conversion: set[str] = set()

                    for inst_id in ordered_instrument_ids:
                        known_instrument = instrument_by_id.get(inst_id)
                        if known_instrument is None:
                            continue
                        currency_code = str(known_instrument.currency)
                        instrument_currency_by_id[inst_id] = currency_code
                        if currency_code != account_currency:
                            currencies_requiring_conversion.add(currency_code)

                    exchange_rate_maps: dict[str, dict[date, Any]] = {
                        currency: {} for currency in currencies_requiring_conversion}
                    initial_exchange_rates: dict[str, Any] = {}

                    if currencies_requiring_conversion:
                        exchange_rate_qs = (
                            ExchangeRate.objects.filter(
                                account=account,
                                convert_to=account.currency,
                                convert_from__in=currencies_requiring_conversion,
                                date__range=(start_date, end_date),
                            )
                            .values('convert_from', 'date', 'exchange_rate_multiplier')
                        )

                        for rate_record in exchange_rate_qs:
                            exchange_rate_maps.setdefault(rate_record['convert_from'], {})[
                                rate_record['date']
                            ] = rate_record['exchange_rate_multiplier']

                        for currency_code in currencies_requiring_conversion:
                            prior_rate = (
                                ExchangeRate.objects.filter(
                                    account=account,
                                    convert_from=currency_code,
                                    convert_to=account.currency,
                                    date__lt=start_date,
                                )
                                .order_by('-date')
                                .values_list('exchange_rate_multiplier', flat=True)
                                .first()
                            )
                            if prior_rate is not None:
                                initial_exchange_rates[currency_code] = prior_rate
                            else:
                                current_rate = CurrentExchangeRate.get_or_create(
                                    account=account,
                                    convert_from=currency_code,
                                    convert_to=account.currency,
                                )
                                if current_rate:
                                    initial_exchange_rates[currency_code] = current_rate.exchange_rate_multiplier

                    price_state = {inst_id: initial_prices.get(inst_id) for inst_id in ordered_instrument_ids}
                    exchange_state = {
                        currency: initial_exchange_rates.get(currency)
                        for currency in currencies_requiring_conversion
                    }

                    value_series_by_instrument: dict[Any, list[Decimal]] = {
                        inst_id: [] for inst_id in ordered_instrument_ids}

                    for date_key in sorted_dates:
                        for inst_id in ordered_instrument_ids:
                            instrument_prices = price_history_map.get(inst_id, {})
                            if date_key in instrument_prices:
                                price_state[inst_id] = instrument_prices[date_key]

                        for currency_code in currencies_requiring_conversion:
                            rate_map = exchange_rate_maps.get(currency_code, {})
                            if date_key in rate_map:
                                exchange_state[currency_code] = rate_map[date_key]

                        quantities_snapshot = quantities_by_date.get(date_key, {})
                        for inst_id in ordered_instrument_ids:
                            quantity = quantities_snapshot.get(inst_id, Decimal('0'))
                            if not quantity:
                                value_series_by_instrument[inst_id].append(Decimal('0'))
                                continue

                            price = price_state.get(inst_id)
                            if price is None:
                                value_series_by_instrument[inst_id].append(Decimal('0'))
                                continue

                            price_decimal = price if isinstance(price, Decimal) else Decimal(price)
                            value = (quantity * price_decimal).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

                            inst_currency = instrument_currency_by_id.get(inst_id)
                            if inst_currency and inst_currency != account_currency:
                                rate = exchange_state.get(inst_currency)
                                if rate is None:
                                    value_series_by_instrument[inst_id].append(Decimal('0'))
                                    continue
                                rate_decimal = rate if isinstance(rate, Decimal) else Decimal(rate)
                                value = (value * rate_decimal).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

                            value_series_by_instrument[inst_id].append(value)

                    if value_series_by_instrument:
                        value_chart_labels = [d.isoformat() for d in sorted_dates]
                        value_chart_datasets = [
                            {
                                'label': instrument_name_by_id.get(inst_id, str(inst_id)),
                                'data': [_decimal_to_float(amount) for amount in series],
                            }
                            for inst_id, series in value_series_by_instrument.items()
                        ]



        if not parcel_labels:
            dashboard_message = (
                f'No active parcels with a remaining value were found for {account.description}.'
            )
            dashboard_message_level = 'info'

    context.update(
        {
            'dashboard_account': account,
            'dashboard_currency': dashboard_currency,
            'tax_settings_warning': _tax_settings_warning(account),
            'data_check_warning': _data_check_warning(account),
            # The buttons, and the state that says whether pressing one is worth it. Only
            # offered where there is a portfolio to act on; every action needs an account.
            'dashboard_action_groups': action_groups(account) if account else [],
            'dashboard_message': dashboard_message,
            'dashboard_message_level': dashboard_message_level,
            'dashboard_portfolio_value_display': total_portfolio_value_display,
            'parcel_total_display': parcel_total_display,
            'parcel_chart_labels': parcel_labels,
            'parcel_chart_values': parcel_values,
            'income_chart_labels': income_labels,
            'income_chart_dividends': dividend_series,
            'income_chart_distributions': distribution_series,
            'area_chart_labels': area_chart_labels,
            'area_chart_datasets': area_chart_datasets,
            'value_chart_labels': value_chart_labels,
            'value_chart_datasets': value_chart_datasets,
        }
    )

    # Never raises, and answers from a cached result for most of the day.
    update_check = version.check_for_update()
    context.update(
        {
            'app_version': update_check['current_version'],
            'latest_version': update_check['latest_version'],
            'release_url': update_check['release_url'],
            'update_available': update_check['update_available'],
        }
    )

    return context


def dashboard_view(request: HttpRequest) -> TemplateResponse:
    context = admin.site.each_context(request)
    app_list = admin.site.get_app_list(request)
    context['app_list'] = app_list
    context['available_apps'] = app_list
    context.setdefault('title', admin.site.index_title)
    context.setdefault('subtitle', None)
    prepare_dashboard_context(request, context)
    request.current_app = admin.site.name
    return TemplateResponse(request, 'admin/dashboard.html', context)





@require_POST
def export_data_view(request: HttpRequest) -> HttpResponseBase:
    """Create a DataExport and return its Excel file as a download.

    Price history is included only if the `include_price_history` option is ticked.
    """
    account = _select_account_for_user(request.user)
    dashboard_url = reverse('admin:dashboard')

    if account is None:
        messages.error(
            request, 'No portfolio is associated with your user, so there is nothing to export.')
        return redirect(dashboard_url)

    include_price_history = bool(request.POST.get('include_price_history'))

    try:
        export = DataExport.objects.create(
            account=account, include_price_history=include_price_history)
        export.refresh_from_db()
    except Exception as exc:
        logger.warning('Export failed for %s: %s', account, exc, exc_info=True)
        messages.error(request, f'Could not build the export: {exc}')
        return redirect(dashboard_url)

    if not export.file:
        messages.error(
            request,
            'The export completed but produced no file. Nothing has been changed; the '
            'attempt is recorded under Data exports.')
        return redirect(dashboard_url)

    return FileResponse(
        export.file.open('rb'),
        as_attachment=True,
        filename=Path(export.file.name or '').name,
    )


@require_POST
def capture_snapshot_view(request: HttpRequest) -> HttpResponse:
    """Take a capital gains snapshot for every fiscal year with a sale.

    Snapshots are not marked lodged; the user ticks that themselves.
    """
    account = _select_account_for_user(request.user)
    dashboard_url = reverse('admin:dashboard')

    if account is None:
        messages.error(
            request,
            'No portfolio is associated with your user, so there is nothing to record.')
        return redirect(dashboard_url)

    year_ids = (
        Sell.objects.filter(account=account, is_active=True)
        .values_list('calculated_fiscal_year', flat=True)
        .distinct()
    )
    fiscal_years = list(
        FiscalYear.objects.filter(id__in=[y for y in year_ids if y]).order_by('start_year')
    )

    if not fiscal_years:
        messages.info(
            request,
            f'{account.description} has no sales yet, so there are no capital gains figures '
            f'to record.')
        return redirect(dashboard_url)

    basis = cgt.residency_basis(account)
    recorded: list[FiscalYear] = []
    try:
        for fiscal_year in fiscal_years:
            try:
                CGTReturnSnapshot.capture(
                    account=account, fiscal_year=fiscal_year, basis=basis)
            except LodgedSnapshotError as exc:
                messages.info(request, str(exc))
                continue
            recorded.append(fiscal_year)
    except Exception as exc:
        logger.warning(
            'Snapshot capture failed for %s: %s', account, exc, exc_info=True)
        messages.error(request, f'Could not record the figures: {exc}')
        return redirect(dashboard_url)

    if not recorded:
        return redirect(dashboard_url)

    names = ', '.join(fiscal_year.name or '' for fiscal_year in recorded)
    note = (
        ' These assume an Australian resident throughout and a flat 50% discount, because '
        'residency has not been declared.'
        if basis == cgt.BASIS_LEGACY else ''
    )
    messages.success(
        request,
        f'Recorded the capital gains figures for {names}. Tick "is lodged" on any of them '
        f'that you have already filed, under CGT return snapshots.{note}')

    return redirect(dashboard_url)


@require_POST
def refresh_prices_view(request: HttpRequest) -> HttpResponse:
    """Refresh prices and exchange rates for the user's portfolio.

    Sets `Account.update_price_history` and saves; a signal does the work.
    """
    account = _select_account_for_user(request.user)
    dashboard_url = reverse('admin:dashboard')

    if account is None:
        messages.error(
            request,
            'No portfolio is associated with your user, so there is nothing to refresh.')
        return redirect(dashboard_url)

    try:
        account.update_price_history = True
        account.save()
    except Exception as exc:  # pragma: no cover - depends on an external provider
        logger.warning(
            'Price refresh failed for %s: %s', account, exc, exc_info=True)
        messages.error(
            request,
            f'Could not refresh prices for {account.description}: {exc}. Your existing '
            f'figures are unchanged.')
        return redirect(dashboard_url)

    latest = _prices_last_updated(account)
    if latest:
        messages.success(
            request,
            f'Prices and exchange rates refreshed for {account.description}. The most '
            f'recent close is {latest.isoformat()}.')
    else:
        messages.warning(
            request,
            f'Refresh finished, but no prices were found for {account.description}. Check '
            f'that its instruments have a market and a ticker that the data provider knows.')

    return redirect(dashboard_url)


@require_POST
def export_cgt_schedule_view(request: HttpRequest) -> HttpResponse:
    """Return the capital gains schedule as an Excel download.

    Built in a temporary file, read into memory and deleted; nothing is stored.
    """
    from share_dinkum_app.reports import cgt_schedule_workbook

    account = _select_account_for_user(request.user)
    dashboard_url = reverse('admin:dashboard')

    if account is None:
        messages.error(
            request,
            'No portfolio is associated with your user, so there is no schedule to export.')
        return redirect(dashboard_url)

    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as handle:
            temp_path = handle.name
        cgt_schedule_workbook(account, temp_path)
        payload = Path(temp_path).read_bytes()
    except Exception as exc:
        logger.warning('CGT schedule export failed for %s: %s', account, exc, exc_info=True)
        messages.error(request, f'Could not build the capital gains schedule: {exc}')
        return redirect(dashboard_url)
    finally:
        Path(temp_path).unlink(missing_ok=True)

    filename = f'CGT_Schedule_{account.description}_{date.today().isoformat()}.xlsx'
    response = HttpResponse(
        payload,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@require_POST
def full_backup_view(request: HttpRequest) -> HttpResponse:
    """Back up the database and media to the backups folder, not as a download."""
    from share_dinkum_app import backup as backup_module

    dashboard_url = reverse('admin:dashboard')
    database = Path(settings.DATABASES['default']['NAME'])
    media = Path(settings.MEDIA_ROOT)

    try:
        result = backup_module.make_backup(database, media, _backup_root())
    except Exception as exc:
        logger.warning('Full backup failed: %s', exc, exc_info=True)
        messages.error(request, f'Could not complete the backup: {exc}')
        return redirect(dashboard_url)

    if result is None:
        messages.info(request, 'There is no data to back up yet.')
        return redirect(dashboard_url)

    messages.success(
        request,
        f'Backed up to {result["path"]} — {result["database_bytes"] / 1024 / 1024:.1f} MB '
        f'database and {result["media_files"]} document(s).')
    return redirect(dashboard_url)


@dataclass(frozen=True)
class ActionOption:
    """A checkbox posted along with an action, such as "include price history"."""

    name: str
    label: str


@dataclass(frozen=True)
class DashboardAction:
    """One dashboard button and everything needed to render and route it.

    * `name` gives the URL name `admin:dashboard_<name>`, used by the template and tests.
    * `status(account)` returns text on when it was last done; `description` says what it does.
    """

    name: str
    route: str
    group: str
    label: str
    description: str
    view: Callable[..., HttpResponseBase]
    status: Callable[[Account | None], str | None] | None = None
    options: tuple[ActionOption, ...] = ()
    primary: bool = False
    #: Button text while the (synchronous) action runs; the button is disabled meanwhile.
    busy_label: str = 'Working...'
    #: True if the response is a file download, so the page must re-enable the button.
    returns_file: bool = False

    @property
    def url_name(self) -> str:
        return f'dashboard_{self.name}'


def _refresh_status(account: Account | None) -> str:
    latest = _prices_last_updated(account)
    return f'Latest close held: {localize(latest)}.' if latest else 'No prices held yet.'


def _snapshot_status(account: Account | None) -> str:
    latest = _snapshot_last_taken(account)
    if not latest:
        return 'No snapshot taken yet.'
    return f'Last snapshot {localize(latest)}.'


def _export_status(account: Account | None) -> str:
    latest = _export_last_taken(account)
    return f'Last exported {localize(latest)}.' if latest else 'Never exported.'


def _backup_root() -> Path:
    """The backup root shared with `uv run update` and the notebook."""
    from share_dinkum_app import backup as backup_module

    return backup_module.DEFAULT_BACKUP_ROOT


def _backup_status(account: Account | None) -> str:
    """When the latest backup was taken, read from the backup folder names."""
    from share_dinkum_app import backup as backup_module

    latest = backup_module.latest_backup(_backup_root())
    if latest is None:
        return 'No backup taken yet.'
    try:
        taken = datetime.strptime(latest.name, '%Y-%m-%dT%H%M%S')
    except ValueError:
        # A folder someone put there by hand. Say what it is called rather than nothing.
        return f'Last backup {latest.name}.'
    # A localised datetime ends in "p.m." already, and a second full stop next to it reads
    # as a typo. A date does not, so the other status lines still add their own.
    text = localize(taken)
    return f'Last backup {text}' + ('' if text.endswith('.') else '.')


#: Every dashboard action, in display order.
DASHBOARD_ACTIONS: tuple[DashboardAction, ...] = (
    DashboardAction(
        name='refresh_prices',
        route='refresh-prices/',
        group='Prices',
        label='Refresh prices',
        description='Fetches prices and exchange rates; can take a minute for a large portfolio.',
        view=refresh_prices_view,
        status=_refresh_status,
        primary=True,
        busy_label='Refreshing...',
    ),
    DashboardAction(
        name='capture_snapshot',
        route='capture-snapshot/',
        group='Tax',
        label='Take capital gains snapshot',
        description='Used later to show whether any figure has moved.',
        view=capture_snapshot_view,
        status=_snapshot_status,
        busy_label='Taking snapshot...',
    ),
    DashboardAction(
        name='export_cgt_schedule',
        route='export-cgt-schedule/',
        group='Tax',
        label='Export Australian CGT report',
        description='Every year in one workbook.',
        view=export_cgt_schedule_view,
        # Deliberately no status. The useful one is whether any year is still a draft, and
        # answering that means building every schedule -- 7.5 seconds on this portfolio, on
        # every dashboard load. Each year carries its own `is_draft` inside the file, which
        # is where it matters, since that is what travels to the accountant.
        status=None,
        busy_label='Building...',
        returns_file=True,
    ),
    DashboardAction(
        name='export',
        route='export/',
        group='Data',
        label='Export portfolio',
        description=('One Excel file of your records, which you can read and load back into '
                     'an empty portfolio. It names your documents but does not contain '
                     'them, and leaves out price history unless asked, since the market can '
                     'supply that again.'),
        view=export_data_view,
        status=_export_status,
        options=(ActionOption(name='include_price_history', label='include price history'),),
        busy_label='Building...',
        returns_file=True,
    ),
    DashboardAction(
        name='full_backup',
        route='full-backup/',
        group='Data',
        label='Full backup',
        description=('Copies the complete database and all attached documents to a separate folder.'),
        view=full_backup_view,
        status=_backup_status,
        busy_label='Backing up...',
    ),
)


def action_groups(account: Account | None) -> list[dict[str, Any]]:
    """`DASHBOARD_ACTIONS` as `[(group, [actions])]`, groups in order of first appearance."""
    groups: list[dict[str, Any]] = []
    index_by_group: dict[str, int] = {}
    for action in DASHBOARD_ACTIONS:
        rendered = {
            'name': action.name,
            'url': reverse(f'admin:{action.url_name}'),
            'label': action.label,
            'description': action.description,
            'status_text': action.status(account) if action.status else None,
            'options': action.options,
            'primary': action.primary,
            'busy_label': action.busy_label,
            'returns_file': action.returns_file,
        }
        if action.group not in index_by_group:
            index_by_group[action.group] = len(groups)
            groups.append({'label': action.group, 'actions': []})
        groups[index_by_group[action.group]]['actions'].append(rendered)
    return groups
