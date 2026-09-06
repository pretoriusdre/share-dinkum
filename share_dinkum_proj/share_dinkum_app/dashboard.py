"""The admin dashboard: what it shows, and the actions it offers.

Split out of `admin.py`, which had grown to hold the model admins and the whole dashboard
at once.

The actions are declared rather than written out. Each one used to cost three separate
edits -- a view here, a line in a patched `admin.site.get_urls`, and a hand-written block in
the template -- so every new button was a fourth copy of the same shape, and the copies had
begun to drift. `DASHBOARD_ACTIONS` is now the single place: the URL, the button, its
grouping and its status line all come from one entry, and `ShareDinkumAdminSite` and the
template both read from it.

What an action must carry is the interesting part. A button on its own is not much use --
the question a person actually has is *whether they need to press it*, so an action names a
`status` callable answering "when was this last done" ("Latest close held: 2026-09-05",
"Never exported.") and a `description` answering "what will this do to me". Both are read
straight off the page rather than remembered.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
import logging
import tempfile

from django.contrib import admin, messages
from django.db.models import Max
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
# Dates used to reach the template as objects and be rendered by it. Now that the
# status line is built here it has to localise them itself, or a date that read
# "Aug. 28, 2026" on the page would silently become an ISO string.
from django.utils.formats import localize
from django.views.decorators.http import require_POST

from djmoney.money import Money

from share_dinkum_app import cgt, version
from share_dinkum_app.choices import TaxpayerType
from share_dinkum_app.models import (
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
    ResidencyPeriod,
    Sell,
)

logger = logging.getLogger(__name__)


def _select_account_for_user(user):
    if not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'visible_account', None)


def _decimal_to_float(value):
    if value is None:
        return 0.0
    if not isinstance(value, Decimal):
        value = Decimal(value)
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _format_money(money):
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


def _export_last_taken(account):
    """When this portfolio was last exported, so the page can say whether it is due."""
    if account is None:
        return None
    latest = (
        DataExport.objects
        .filter(account=account, is_active=True)
        .order_by('-created_at')
        .first()
    )
    return latest.created_at.date() if latest else None


def _snapshot_last_taken(account):
    """When this portfolio last had a capital gains snapshot taken.

    Shown beside the button for the same reason as the price date: whether it is worth
    pressing is a question the page can answer.
    """
    if account is None:
        return None
    return (
        CGTReturnSnapshot.objects
        .filter(account=account, is_active=True)
        .aggregate(latest=Max('taken_at'))['latest']
    )


def _prices_last_updated(account):
    """The most recent day this portfolio has a closing price for.

    Shown next to the refresh button so the answer to "do I need to press this?" is on the
    page rather than in the user's head.
    """
    if account is None:
        return None
    return (
        InstrumentPriceHistory.objects
        .filter(account=account)
        .aggregate(latest=Max('date'))['latest']
    )


def _tax_settings_warning(account):
    """Whether this account's capital gains rest on an assumption nobody has confirmed.

    Only raised once there is something at stake -- an account with no sales has no capital
    gain to get wrong -- and it goes quiet permanently once the user has been to the tax
    settings, whatever they chose there. A banner that cannot be dismissed by answering it
    trains people to ignore banners.
    """
    if account is None or account.tax_settings_reviewed_at is not None:
        return None
    if not Sell.objects.filter(account=account, is_active=True).exists():
        return None

    missing = []
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


def prepare_dashboard_context(request, context):
    account = _select_account_for_user(request.user)

    dashboard_message = None
    dashboard_message_level = 'info'
    total_portfolio_value_display = None
    parcel_labels = []
    parcel_values = []
    #: Summed from the slices actually drawn rather than read off the account, so the
    #: caption can never disagree with the chart above it. The two are normally the same
    #: figure, but an instrument whose conversion fails is skipped for the chart and would
    #: still count towards the account total -- and a total that does not add up to its own
    #: parts is worse than no total at all.
    parcel_total = Decimal('0')
    parcel_total_display = None
    income_labels = []
    dividend_series = []
    distribution_series = []
    area_chart_labels = []
    area_chart_datasets = []
    value_chart_labels = []
    value_chart_datasets = []
    dashboard_currency = None


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

        income_by_year = {}

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
            trade_adjustments = defaultdict(dict)

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
                dataset_values = {inst_id: [] for inst_id in ordered_instrument_ids}
                quantities_by_date = {}

                for date_key in sorted_dates:
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

                    price_history_map = defaultdict(dict)
                    price_history_qs = (
                        InstrumentPriceHistory.objects.filter(
                            account=account,
                            instrument_id__in=ordered_instrument_ids,
                            date__range=(start_date, end_date),
                        )
                        .values('instrument_id', 'date', 'close')
                    )
                    for record in price_history_qs:
                        price_history_map[record['instrument_id']][record['date']] = record['close']

                    initial_prices = {}
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
                    instrument_currency_by_id = {}
                    currencies_requiring_conversion = set()

                    for inst_id in ordered_instrument_ids:
                        instrument_obj = instrument_by_id.get(inst_id)
                        if instrument_obj is None:
                            continue
                        currency_code = str(instrument_obj.currency)
                        instrument_currency_by_id[inst_id] = currency_code
                        if currency_code != account_currency:
                            currencies_requiring_conversion.add(currency_code)

                    exchange_rate_maps = {currency: {} for currency in currencies_requiring_conversion}
                    initial_exchange_rates = {}

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

                        for record in exchange_rate_qs:
                            exchange_rate_maps.setdefault(record['convert_from'], {})[
                                record['date']
                            ] = record['exchange_rate_multiplier']

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

                    value_series_by_instrument = {inst_id: [] for inst_id in ordered_instrument_ids}

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

                            currency_code = instrument_currency_by_id.get(inst_id)
                            if currency_code and currency_code != account_currency:
                                rate = exchange_state.get(currency_code)
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


def dashboard_view(request):
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
def export_data_view(request):
    """Build a full Excel export of the portfolio and hand it straight back.

    Exporting meant opening Data exports, adding a record, saving it, and then finding the
    file on the record that the save had generated. The record is worth keeping -- it is the
    history of what was exported and when -- but it should not be the interface.

    The file is streamed as the response rather than being linked to. A link would depend on
    media being served, which differs between a local run and anything behind a real web
    server, and would hand out a URL to a file containing the whole portfolio.

    Price history is excluded unless asked for. It is by far the largest table and it is
    reconstructible from the market, which the rest of the file is not.
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
        filename=Path(export.file.name).name,
    )


@require_POST
def capture_snapshot_view(request):
    """Record the capital gains figures for every year that has a sale.

    Capital gains are worked out on demand and never stored, so improving a calculation
    changes what the application says about a year that may already have been filed. This is
    the record of what it said beforehand, and the only way to create one: the figures come
    from the report rather than from anything typed, and the model will not let them be
    edited afterwards.

    Snapshots are deliberately **not** marked as lodged here. Whether a set of figures was
    actually filed with the ATO is a claim about the outside world that the application has
    no way to verify, so it stays a box the user ticks themselves.
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
    try:
        for fiscal_year in fiscal_years:
            CGTReturnSnapshot.capture(
                account=account, fiscal_year=fiscal_year, basis=basis)
    except Exception as exc:
        logger.warning(
            'Snapshot capture failed for %s: %s', account, exc, exc_info=True)
        messages.error(request, f'Could not record the figures: {exc}')
        return redirect(dashboard_url)

    names = ', '.join(fiscal_year.name for fiscal_year in fiscal_years)
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
def refresh_prices_view(request):
    """Refresh prices and exchange rates for the portfolio the user is looking at.

    This exists because the only way to do it was to open the account, tick a checkbox
    called "update price history", and save -- at which point a signal did the work and
    unticked it again. That is a button wearing a field's clothing, and nobody found it.

    POST only, because it reaches out to a market data provider and writes. A GET would let
    a link, a prefetch or a refresh trigger it, and the request is slow enough that firing it
    twice is worse than merely wasteful.

    The work itself is unchanged: this sets the same flag and saves, so there is one
    implementation of "refresh this portfolio" rather than two that can drift.
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
def export_cgt_schedule_view(request):
    """Hand back the capital gains schedule as a workbook.

    Built into a temporary file and returned as bytes rather than streamed from disk. The
    portfolio export streams from the `DataExport` record it creates, but there is no record
    to create here -- a schedule is derived and reproducible, so storing one would be
    keeping a stale copy of something the application can always work out again. That leaves
    only a temp file, whose handle would have to outlive the response; reading ~50KB into
    memory avoids the question entirely.
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


@dataclass(frozen=True)
class ActionOption:
    """A checkbox posted along with an action, such as "include price history"."""

    name: str
    label: str


@dataclass(frozen=True)
class DashboardAction:
    """One button on the dashboard, and everything the page needs to render it.

    `name` becomes the URL name as `admin:dashboard_<name>`. Those names are reversed by the
    template and by the tests, so they are part of the interface and not free to change.

    `status` is given the account and returns whatever the page should say about when this
    was last done. It is separate from `description` on purpose: the description is fixed
    text about what the button does, while the status is the changing fact that tells you
    whether to press it at all.
    """

    name: str
    route: str
    group: str
    label: str
    description: str
    view: Callable
    status: Callable | None = None
    options: tuple[ActionOption, ...] = ()
    primary: bool = False
    #: What the button says while it is working. These actions run synchronously and can
    #: take a while, so a button that still looks clickable invites a second press and a
    #: second full run.
    busy_label: str = 'Working...'
    #: True where the response is a file rather than a page. Such a form never navigates,
    #: so a disabled button would stay disabled for good and has to be put back by hand.
    returns_file: bool = False

    @property
    def url_name(self):
        return f'dashboard_{self.name}'


def _refresh_status(account):
    latest = _prices_last_updated(account)
    return f'Latest close held: {localize(latest)}.' if latest else 'No prices held yet.'


def _snapshot_status(account):
    latest = _snapshot_last_taken(account)
    if not latest:
        return 'No snapshot taken yet.'
    return f'Last snapshot {localize(latest)}.'


def _export_status(account):
    latest = _export_last_taken(account)
    return f'Last exported {localize(latest)}.' if latest else 'Never exported.'


#: Every action the dashboard offers, in the order it is shown. Grouped so that five of them
#: read as three short lists rather than one long one; the groups are the kinds of thing a
#: person comes to the dashboard to do, not the models involved.
DASHBOARD_ACTIONS = (
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
        description=('Take one before changing your tax settings, so any figure that moves '
                     'can be explained.'),
        view=capture_snapshot_view,
        status=_snapshot_status,
        busy_label='Taking snapshot...',
    ),
    DashboardAction(
        name='export_cgt_schedule',
        route='export-cgt-schedule/',
        group='Tax',
        label='Export capital gains schedule',
        description=('Every year in one workbook: the figures a return asks for, the '
                     'categories behind them, every event, and whether each year is final.'),
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
        description=('An Excel file of everything, which loads back into an empty portfolio. '
                     'This is your backup.'),
        view=export_data_view,
        status=_export_status,
        options=(ActionOption(name='include_price_history', label='include price history'),),
        busy_label='Building...',
        returns_file=True,
    ),
)


def action_groups(account):
    """The actions arranged for the template: an ordered list of (group, [actions]).

    Built here rather than in the template so that the template loops over data instead of
    deciding anything. Group order follows first appearance in DASHBOARD_ACTIONS, so the
    order of the tuple above is the order on the page.
    """
    groups = []
    index_by_group = {}
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
