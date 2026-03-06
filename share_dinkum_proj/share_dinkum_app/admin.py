from django.contrib import admin


#import share_dinkum_app.models



from django.apps import apps

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm

from django.db import models
from django.db.models import Model, ForeignKey, Min

from django.db.models.fields.reverse_related import ManyToManyRel
from django.db.models import ManyToManyRel, ManyToManyField
from django.template.response import TemplateResponse
from django.urls import path

import share_dinkum_app

import share_dinkum_app.admin
import share_dinkum_app.models

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
    Sell,
)



from collections import defaultdict
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


    def get_inline_instances(self, request, obj=None):

        inline_instances = super().get_inline_instances(request, obj)
        
        #added_inlines = set()

        if obj is not None:

            for rel in self.model._meta.related_objects:
                related_model = rel.related_model
                if related_model == share_dinkum_app.models.AppUser:
                    continue
                related_manager_name = rel.get_accessor_name()
                related_manager = getattr(obj, related_manager_name)
                related_count = related_manager.count()
                # Don't show the Inline if there are more than 200 related objects, due to loading speed concerns.
                if related_count < 200:
                    # We only want to ManyToOneRel to the through tables.
                    if isinstance(rel, ManyToManyRel):
                        continue
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
    account = getattr(user, 'default_account', None)
    if account:
        return account
    return Account.objects.filter(owner=user).order_by('created_at').first()


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
                sorted_dates = sorted(available_dates)

                instrument_name_by_id = {instrument.id: instrument.name for instrument in instruments}

                missing_instrument_ids = [
                    inst_id
                    for inst_id in area_instrument_ids
                    if inst_id not in instrument_name_by_id
                ]
                if missing_instrument_ids:
                    for instrument_obj in Instrument.objects.filter(account=account, id__in=missing_instrument_ids):
                        instrument_name_by_id[instrument_obj.id] = instrument_obj.name

                ordered_instrument_ids = sorted(
                    area_instrument_ids,
                    key=lambda inst_id: instrument_name_by_id.get(inst_id, ''),
                )

                quantities_current = {inst_id: Decimal('0') for inst_id in ordered_instrument_ids}
                dataset_values = {inst_id: [] for inst_id in ordered_instrument_ids}

                for date_key in sorted_dates:
                    adjustments = trade_adjustments.get(date_key, {})
                    for inst_id, delta in adjustments.items():
                        quantities_current[inst_id] = (
                            quantities_current[inst_id] + delta
                        ).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

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


        if not parcel_labels:
            dashboard_message = (
                f'No active parcels with a remaining value were found for {account.description}.'
            )
            dashboard_message_level = 'info'

    context.update(
        {
            'dashboard_account': account,
            'dashboard_currency': dashboard_currency,
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





if not getattr(admin.site, '_dashboard_url_included', False):
    original_get_urls = admin.site.get_urls

    def get_urls():
        urls = original_get_urls()
        custom_urls = [
            path('dashboard/', admin.site.admin_view(dashboard_view), name='dashboard'),
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


# Map specific models to custom admin if required, or hide them.

model_admin_map = {

    Account : AccountAdmin,
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

