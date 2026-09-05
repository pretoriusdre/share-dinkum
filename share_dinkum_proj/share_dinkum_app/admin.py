from django.contrib import admin
from django import forms


#import share_dinkum_app.models



from django.apps import apps

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Model, ForeignKey, Max, Min

from django.db.models.fields.reverse_related import ManyToManyRel
from django.db.models import ManyToManyRel, ManyToManyField
from django.template.response import TemplateResponse
from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.views.decorators.http import require_POST

import share_dinkum_app

import share_dinkum_app.admin
import share_dinkum_app.models

from share_dinkum_app import cgt, version
from share_dinkum_app.choices import LegalForm, LegalFormSource, TaxpayerType

from share_dinkum_app.models import (
    AppUser,
    Account,
    Parcel,
    Buy,
    Instrument,
    Dividend,
    Distribution,
    InstrumentPriceHistory,
    ExchangeRate,
    CurrentExchangeRate,
    CGTReturnSnapshot,
    DataExport,
    FiscalYear,
    ResidencyPeriod,
    Sell,
)



from collections import defaultdict
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging
from types import MethodType

logger = logging.getLogger(__name__)





class BaseInline(admin.TabularInline):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exclude = self.get_excluded_fields()
        #self.autocomplete_fields = self.get_autocomplete_fields()

    def get_autocomplete_fields(self, request=None, obj=None):
        return [field.name for field in self.model._meta.get_fields() if isinstance(field, ForeignKey)]

    def get_excluded_fields(self):
        excluded_fields = ['notes']  # Add fields you want to exclude
        return [
            field.name
            for field in self.model._meta.get_fields()
            if field.name in excluded_fields
        ]
    
    extra = 1



class GenericModelAdmin(admin.ModelAdmin):

    search_fields = ('id',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.autocomplete_fields = self.get_autocomplete_fields()
        self.list_display = self.get_list_display_fields()
        self.list_filter = self.get_list_filter_fields()


        if hasattr(self.model, 'name'):
            self.search_fields = getattr(self, 'search_fields', ()) + ('name',)

        if hasattr(self.model, 'description'):
            self.search_fields = getattr(self, 'search_fields', ()) + ('description',)


    def get_autocomplete_fields(self, request=None, obj=None):

        return [field.name for field in self.model._meta.get_fields() if isinstance(field, ForeignKey)]
    

    def get_fields(self, request, obj=None):
        hidden_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']

        form = self._get_form_for_get_fields(request, obj)


        # all_fields =  ['id'] + [*form.base_fields] 
        # calculated_fields = [field.name for field in self.model._meta.fields if field.name.startswith('calculated_')]
        # calculated_fields = [name for name in calculated_fields if not name.endswith('_currency')]

        all_fields = [field.name for field in self.model._meta.fields if not field.name.endswith('_currency')]

        return [field for field in all_fields if field not in hidden_fields]
    

    
    def get_readonly_fields(self, request, obj=None):

        non_editable_fields = [field.name for field in self.model._meta.fields if not field.editable]
        non_editable_fields = [name for name in non_editable_fields if not name.endswith('_currency')]
        readonly_fields = non_editable_fields

        return readonly_fields
    


    def get_list_display_fields(self, request=None, obj=None):
        excluded_names = ['created_at', 'created_by', 'updated_at', 'updated_by', 'notes',  'unit_price_currency', 'total_brokerage_currency', '_creation_handled']
        fields = [
            field.name
            for field in self.model._meta.get_fields()
            if not (field.many_to_many or field.one_to_many or field.one_to_one)
            and field.name not in excluded_names
        ]
        return fields
        
    def get_list_filter_fields(self, request=None, obj=None):
        filterable_fields = ['instrument', 'account']
        return [field.name for field in self.model._meta.get_fields() if field.name in filterable_fields]
    
    def save_model(self, request, obj, form, change):
        obj.save(user=request.user)  # Ensure the user is passed
        super().save_model(request, obj, form, change)


    #: Roughly how many form fields all the inlines on one change page may add up to.
    #: The 200-row rule below is about how long a page takes to build; this is about
    #: whether it can be submitted at all. A form field is a POST parameter, and Django
    #: refuses a submission with too many of them -- so a page over the limit renders
    #: perfectly, then raises TooManyFieldsSent when you press Save, which reads as the
    #: save being broken rather than the page being too big. Rows are the wrong unit for
    #: that: 35 dividends carry more fields than 100 of something narrow.
    INLINE_FIELD_BUDGET = 6000

    def get_inline_instances(self, request, obj=None):

        inline_instances = super().get_inline_instances(request, obj)

        #added_inlines = set()

        if obj is not None:
            remaining_fields = self.INLINE_FIELD_BUDGET

            for rel in self.model._meta.related_objects:
                related_model = rel.related_model
                if related_model == share_dinkum_app.models.AppUser:
                    continue
                related_manager_name = rel.get_accessor_name()

                # A reverse one-to-one is an object, not a manager, and accessing it raises
                # when there is nothing on the other side -- unlike a reverse foreign key,
                # which just gives an empty manager. Every CostBaseAdjustment without an
                # AttributionStatement made this page a 500, which is all of them until one
                # is linked. The absent side is exactly when you would open the page to
                # create it.
                if rel.one_to_one:
                    try:
                        getattr(obj, related_manager_name)
                        related_count = 1
                    except ObjectDoesNotExist:
                        related_count = 0
                else:
                    related_manager = getattr(obj, related_manager_name)
                    related_count = related_manager.count()

                # Don't show the Inline if there are more than 200 related objects, due to loading speed concerns.
                if related_count < 200:
                    # We only want to ManyToOneRel to the through tables.
                    if isinstance(rel, ManyToManyRel):
                        continue

                    # What this inline will cost in form fields, near enough: one per
                    # editable field per row, plus the blank rows the formset adds.
                    editable = sum(1 for f in related_model._meta.fields if f.editable)
                    cost = (related_count + BaseInline.extra) * editable
                    if cost > remaining_fields:
                        continue
                    remaining_fields -= cost

                    inline = type('DynamicInline', (BaseInline,), {'model': related_model})
                    #if related_model not in added_inlines:
                    inline_instances.append(inline(self.model, self.admin_site))
                        #added_inlines.add(related_model)

        return inline_instances
    
    # Set the default account to the current user's default account if it exists.
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        current_user = request.user
        if current_user and hasattr(current_user, 'default_account'):
            try:
                form.base_fields['account'].initial = current_user.default_account
            except Exception:
                pass
            #if hasattr(form.base_fields, 'account'):
                
        return form
    

class GenericModelAdminWithoutAdd(GenericModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return True


class HiddenModelAdmin(admin.ModelAdmin):
    search_fields = ('id', 'description')
    def has_module_permission(self, request):
        return False  # hides from sidebar



class AppUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = AppUser

class AppUserAdmin(UserAdmin):
    form = AppUserChangeForm

    fieldsets = UserAdmin.fieldsets + (
            (None, {'fields': ('default_account',)}),
    )


class AccountAdmin(admin.ModelAdmin):
    search_fields = ('id', 'description')


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
    """When capital gains figures were last recorded for this portfolio.

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


def _prepare_dashboard_context(request, context):
    account = _select_account_for_user(request.user)

    dashboard_message = None
    dashboard_message_level = 'info'
    total_portfolio_value_display = None
    parcel_labels = []
    parcel_values = []
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
            'prices_last_updated': _prices_last_updated(account),
            'snapshot_last_taken': _snapshot_last_taken(account),
            'export_last_taken': _export_last_taken(account),
            'dashboard_message': dashboard_message,
            'dashboard_message_level': dashboard_message_level,
            'dashboard_portfolio_value_display': total_portfolio_value_display,
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
    _prepare_dashboard_context(request, context)
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


if not getattr(admin.site, '_dashboard_url_included', False):
    original_get_urls = admin.site.get_urls

    def get_urls():
        urls = original_get_urls()
        custom_urls = [
            path('dashboard/', admin.site.admin_view(dashboard_view), name='dashboard'),
            path(
                'dashboard/refresh-prices/',
                admin.site.admin_view(refresh_prices_view),
                name='dashboard_refresh_prices',
            ),
            path(
                'dashboard/capture-snapshot/',
                admin.site.admin_view(capture_snapshot_view),
                name='dashboard_capture_snapshot',
            ),
            path(
                'dashboard/export/',
                admin.site.admin_view(export_data_view),
                name='dashboard_export',
            ),
        ]
        return custom_urls + urls

    admin.site.get_urls = get_urls
    admin.site._dashboard_url_included = True


if not getattr(admin.site, '_dashboard_index_overridden', False):

    def _dashboard_index(self, request, extra_context=None):
        app_list = self.get_app_list(request)
        context = {
            **self.each_context(request),
            'title': self.index_title,
            'subtitle': None,
            'app_list': app_list,
            'available_apps': app_list,
        }
        if extra_context:
            context.update(extra_context)
        _prepare_dashboard_context(request, context)
        request.current_app = self.name
        template = self.index_template or 'admin/dashboard.html'
        return TemplateResponse(request, template, context)

    admin.site.index = MethodType(_dashboard_index, admin.site)
    admin.site._dashboard_index_overridden = True


CONFIRM_LEGAL_FORM_FIELD = 'confirm_legal_form'


class UnsetNullBooleanSelect(forms.NullBooleanSelect):
    """A nullable boolean labelled by what each state does, not by what it asserts.

    Django's labels are Unknown / Yes / No. Both halves of that mislead here.

    "Unknown" describes a fact nobody has established yet, so it asks to be resolved. For
    an override the empty state is not an open question -- it is the working setting, and
    the one that lets the answer be derived per parcel.

    "No" is worse, because it is true. An ordinary listed share is not taxable Australian
    property in its own right, so answering honestly is exactly what a careful person does
    -- and it silently overrides the s104-165(3) departure deeming, which is the main route
    by which such a share becomes taxable Australian property. Set across a portfolio it
    reads every capital gain as disregarded. Saying what the option does, rather than what
    it asserts, is the difference between a true answer and an informed one.

    Only the labels change. The submitted values stay unknown/true/false, so
    `NullBooleanSelect.value_from_datadict` still round-trips None correctly.
    """

    def __init__(self, attrs=None):
        super().__init__(attrs)
        self.choices = [
            ('unknown', 'Unset - derive it per parcel'),
            ('true', 'Yes - always taxable Australian property'),
            ('false', 'No - never, overriding the departure deeming'),
        ]


class InstrumentAdminForm(forms.ModelForm):
    """Adds the one thing `Instrument.save()` cannot express: agreeing with a suggestion.

    `Instrument.save()` promotes `legal_form_source` to USER only when the legal form
    *changes*, which covers correcting a suggestion and creating an instrument that already
    carries one. It cannot cover the commonest case of all -- the suggestion is right, and
    you want to say so -- because nothing changes, so nothing is recorded and the schedule
    goes on calling itself a draft with no indication why.

    Making it a tick rather than inferring it from a save is deliberate. The alternative was
    to treat any save as confirmation, which would mean editing an unrelated field on the
    same form silently answers a tax question on the user's behalf.
    """

    confirm_legal_form = forms.BooleanField(
        required=False,
        label='Confirm legal form',
        help_text='Tick to record this legal form as your answer rather than a suggestion. '
                  'A capital gains schedule stays a draft while any instrument on it rests '
                  'on a suggestion.',
    )

    class Meta:
        model = Instrument
        fields = '__all__'
        widgets = {
            'is_taxable_australian_property_override': UnsetNullBooleanSelect(),
        }


class InstrumentAdmin(GenericModelAdmin):
    """Instruments, plus an explicit way to confirm what one legally is."""

    form = InstrumentAdminForm
    actions = ['confirm_legal_form_action']

    def get_fields(self, request, obj=None):
        """Offer the tick only where there is something to confirm.

        Once the form is confirmed the readonly `legal_form_source` says so, and a tick that
        could be un-ticked would raise a question this deliberately does not answer: whether
        clearing it should demote a confirmed answer back to a suggestion.
        """
        fields = list(super().get_fields(request, obj))
        if obj is None or obj.is_classified or obj.legal_form == LegalForm.UNKNOWN:
            return [name for name in fields if name != CONFIRM_LEGAL_FORM_FIELD]
        if CONFIRM_LEGAL_FORM_FIELD not in fields:
            position = (fields.index('legal_form_source') + 1
                        if 'legal_form_source' in fields else len(fields))
            fields.insert(position, CONFIRM_LEGAL_FORM_FIELD)
        return fields

    def save_model(self, request, obj, form, change):
        """Apply the tick before saving, so `Instrument.save()` sees it as caller-set."""
        if form.cleaned_data.get(CONFIRM_LEGAL_FORM_FIELD):
            if obj.legal_form == LegalForm.UNKNOWN:
                messages.warning(
                    request,
                    'Nothing to confirm: set a legal form other than '
                    f'"{LegalForm.UNKNOWN.label}" first.',
                )
            else:
                obj.legal_form_source = LegalFormSource.USER
        super().save_model(request, obj, form, change)

    @admin.action(description='Confirm legal form as your answer')
    def confirm_legal_form_action(self, request, queryset):
        """Confirm in bulk, which is how a back catalogue of closed positions gets done.

        Anything still unclassified is named rather than skipped quietly: it is the one
        outcome where the user's selection did not do what they asked.
        """
        confirmed = 0
        already = 0
        unclassified = []

        for instrument in queryset:
            if instrument.legal_form == LegalForm.UNKNOWN:
                unclassified.append(instrument.name)
            elif instrument.legal_form_source == LegalFormSource.USER:
                already += 1
            else:
                instrument.legal_form_source = LegalFormSource.USER
                instrument.save(user=request.user)
                confirmed += 1

        if confirmed:
            messages.success(request, f'Confirmed the legal form of {confirmed} instrument(s).')
        if already:
            messages.info(request, f'{already} instrument(s) were already confirmed.')
        if unclassified:
            messages.warning(
                request,
                f'{len(unclassified)} instrument(s) have no legal form set, so there was '
                f'nothing to confirm: {", ".join(sorted(unclassified))}.',
            )


# Map specific models to custom admin if required, or hide them.

model_admin_map = {

    Account : AccountAdmin,
    Instrument : InstrumentAdmin,
    AppUser : AppUserAdmin,
    Group : HiddenModelAdmin,
    ContentType : HiddenModelAdmin,
    Parcel : GenericModelAdminWithoutAdd,

}

try:
    admin.site.unregister(Group)
except admin.sites.NotRegistered:
    pass


for model in apps.get_app_config('share_dinkum_app').get_models():

    model_admin_map.setdefault(model, GenericModelAdmin)

for model, model_admin in model_admin_map.items():
    try:
        if model_admin and issubclass(model, Model):
            admin.site.register(model, model_admin)
    except admin.sites.AlreadyRegistered:
        logger.error(f'Failed to register {model} with {model_admin}. Already registered?')



without_add = ['Parcel']

