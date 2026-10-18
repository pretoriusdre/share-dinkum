from typing import Any

from django.contrib import admin, messages
from django.contrib.admin import helpers
from django import forms

from django.apps import apps
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.admin import UserAdmin
from django.contrib.admin.exceptions import AlreadyRegistered, NotRegistered
from django.contrib.admin.options import InlineModelAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Model, ForeignKey, ManyToManyRel, QuerySet
from django.http import HttpRequest, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.text import capfirst

import share_dinkum_app
import share_dinkum_app.models

from share_dinkum_app import portfolio_scope
from share_dinkum_app.choices import LegalForm, LegalFormSource
from share_dinkum_app.models import (
    AppUser,
    Account,
    CostBaseAdjustment,
    CPIIndex,
    ExchangeRate,
    FiscalYear,
    FiscalYearType,
    Parcel,
    Instrument,
)

import logging

logger = logging.getLogger(__name__)





class PortfolioScopedMixin:
    """Lists, opens and links only the selected portfolio's records, and puts new ones in it.

    For a model with an `account` field; the shared tables (fiscal years, CPI) pass through.
    Django's autocomplete asks the linked model's admin for its rows, so pickers follow too.
    """

    model: Any

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        queryset = super().get_queryset(request)  # type: ignore[misc]
        if not portfolio_scope.is_scoped(self.model):
            return queryset
        account = portfolio_scope.current_portfolio(request)
        return queryset.filter(account=account) if account is not None else queryset.none()

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        account = portfolio_scope.current_portfolio(request)
        related = db_field.related_model
        if db_field.name == 'account' and related is Account and portfolio_scope.is_scoped(self.model):
            # Shown, but not chosen: a record goes into the portfolio selected. Kept on the form
            # (disabled, not excluded) so constraints including it, such as a unique instrument
            # name, are checked by the form instead of failing as the row is written.
            kwargs['queryset'] = (Account.objects.filter(pk=account.pk) if account is not None
                                  else Account.objects.none())
            kwargs['initial'] = account
            kwargs['disabled'] = True
        elif portfolio_scope.is_scoped(related):
            # The form's own check, so another portfolio's id fails whatever the widget sent.
            kwargs['queryset'] = related._default_manager.filter(
                account=account) if account is not None else related._default_manager.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)  # type: ignore[misc]


class BaseInline(PortfolioScopedMixin, admin.TabularInline):
    
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exclude = self.get_excluded_fields()  # type: ignore[misc]
        #self.autocomplete_fields = self.get_autocomplete_fields()

    def get_autocomplete_fields(self, request: HttpRequest | None = None, obj: Any = None) -> list[str]:
        return [field.name for field in self.model._meta.get_fields() if isinstance(field, ForeignKey)]

    def get_excluded_fields(self) -> list[str]:
        excluded_fields = ['notes']  # Add fields you want to exclude
        return [
            field.name
            for field in self.model._meta.get_fields()
            if field.name in excluded_fields
        ]
    
    extra = 1



class GenericModelAdmin(PortfolioScopedMixin, admin.ModelAdmin):

    search_fields = ('id',)

    def _in_other_portfolio(self, request: HttpRequest, object_id: str) -> Account | None:
        """The portfolio `object_id` is in, if the user may see it but it is not the selected one."""
        if not portfolio_scope.is_scoped(self.model):
            return None
        try:
            account_id = self.model._default_manager.filter(pk=object_id).values_list(
                'account_id', flat=True).first()
        except Exception:  # Not an id of this table at all.
            return None
        current = portfolio_scope.current_portfolio(request)
        if account_id is None or (current is not None and account_id == current.pk):
            return None
        user: Any = request.user
        found: Account | None = user.portfolios().filter(pk=account_id).first()
        return found

    def _other_portfolio_redirect(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect | None:
        """Back to the list, saying where the record is, if it is in another of the user's portfolios."""
        elsewhere = self._in_other_portfolio(request, object_id)
        if elsewhere is None:
            return None
        opts = self.model._meta
        messages.warning(
            request,
            f'This {opts.verbose_name} is in the portfolio "{elsewhere.description}". Select it '
            f'at the top of the page to open it.')
        return HttpResponseRedirect(reverse(f'admin:{opts.app_label}_{opts.model_name}_changelist'))

    def change_view(self, request: HttpRequest, object_id: str, form_url: str = '',
                    extra_context: dict[str, Any] | None = None) -> Any:
        return (self._other_portfolio_redirect(request, object_id)
                or super().change_view(request, object_id, form_url, extra_context))

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        self.mention_kept_spreads(request, obj)

    # A change the holding cannot follow is refused as it saves, after the form has validated,
    # and the save has been rolled back. Say why on the same page rather than failing.
    def _refusal_shown(self, request: HttpRequest, view: Any, *args: Any, **kwargs: Any) -> Any:
        from share_dinkum_app.holdings import live

        try:
            return view(request, *args, **kwargs)
        except live.HoldingsRefused as refusal:
            messages.error(request, str(refusal))
            return HttpResponseRedirect(request.get_full_path())

    def changeform_view(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        return self._refusal_shown(request, super().changeform_view, *args, **kwargs)

    def delete_view(self, request: HttpRequest, object_id: str, *args: Any, **kwargs: Any) -> Any:
        return (self._other_portfolio_redirect(request, object_id)
                or self._refusal_shown(request, super().delete_view, object_id, *args, **kwargs))

    def changelist_view(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        return self._refusal_shown(request, super().changelist_view, *args, **kwargs)

    def get_deleted_objects(self, objs: Any, request: HttpRequest) -> Any:
        """Let a buy's delete take its parcels, which cannot be deleted on their own, and its
        share of adjustments, which `Buy.delete` reallocates."""
        from share_dinkum_app.holdings import live
        from share_dinkum_app.models import Buy, CostBaseAdjustmentAllocation

        from share_dinkum_app.choices import AllocationMethod

        deleted, counts, perms_needed, protected = super().get_deleted_objects(objs, request)
        if self.model is Buy and live.active() and not CostBaseAdjustmentAllocation.objects.filter(
                parcel__buy__in=[obj.pk for obj in objs]).exclude(
                cost_base_adjustment__allocation_method=AllocationMethod.QTY_HELD).exists():
            # A manually allocated share has no rule to go by, so it still blocks the delete.
            share = f'{capfirst(CostBaseAdjustmentAllocation._meta.verbose_name)}: '
            protected = [entry for entry in protected if not str(entry).startswith(share)]
        return deleted, counts, perms_needed - DELETED_WITH_THEIR_SOURCE, protected

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        super().delete_model(request, obj)
        self.mention_reallocated(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        """One at a time, so each buy's own delete reallocates its share of adjustments."""
        from share_dinkum_app.models import Buy

        if self.model is not Buy:
            super().delete_queryset(request, queryset)
            return
        for obj in queryset:
            obj.delete()
            self.mention_reallocated(request, obj)

    @staticmethod
    def mention_reallocated(request: HttpRequest, obj: Any) -> None:
        from share_dinkum_app.models import CostBaseAdjustment

        ids = getattr(obj, '_reallocate', None)
        if ids:
            years = ', '.join(str(year) for year in CostBaseAdjustment.objects.filter(
                pk__in=ids).order_by('financial_year_end_date').values_list('financial_year_end_date', flat=True))
            messages.info(
                request,
                f'Its share of the cost base adjustments for the years ending {years} was '
                f'reallocated over the rest of the holding.')

    @staticmethod
    def mention_kept_spreads(request: HttpRequest, obj: Any) -> None:
        """A trade dated in a year whose adjustment was already spread leaves that spread as it is."""
        from share_dinkum_app.choices import AllocationMethod
        from share_dinkum_app.holdings import live
        from share_dinkum_app.models import Buy, CostBaseAdjustment, Sell

        if not live.active() or not isinstance(obj, (Buy, Sell)):
            return
        kept = CostBaseAdjustment.objects.filter(
            account_id=obj.account_id, instrument_id=obj.instrument_id, _creation_handled=True,
            allocation_method=AllocationMethod.QTY_HELD, financial_year_end_date__gte=obj.date,
        ).order_by('financial_year_end_date')
        if kept:
            years = ', '.join(str(adjustment.financial_year_end_date) for adjustment in kept)
            messages.info(
                request,
                f'Cost base adjustments for the years ending {years} were spread before this was '
                f'entered, and are kept as they were. To spread them over the holding as it now '
                f'stands, select them under Cost base adjustments and choose "Reallocate".')

    # Set on the instance rather than the class, since they are worked out from the model.
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.autocomplete_fields = self.get_autocomplete_fields()  # type: ignore[misc]
        self.list_display = self.get_list_display_fields()  # type: ignore[assignment]
        self.list_filter = self.get_list_filter_fields()  # type: ignore[misc, assignment]


        if hasattr(self.model, 'name'):
            self.search_fields = getattr(self, 'search_fields', ()) + ('name',)  # type: ignore[misc, assignment]

        if hasattr(self.model, 'description'):
            self.search_fields = getattr(self, 'search_fields', ()) + ('description',)  # type: ignore[misc, assignment]


    def get_autocomplete_fields(self, request: HttpRequest | None = None, obj: Any = None) -> list[str]:
        # The account is fixed to the selected portfolio, so it needs no search box.
        return [field.name for field in self.model._meta.get_fields() if isinstance(field, ForeignKey)
                and not (field.name == 'account' and portfolio_scope.is_scoped(self.model))]


    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:  # type: ignore[override]
        hidden_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']

        form = self._get_form_for_get_fields(request, obj)  # type: ignore[attr-defined]


        # all_fields =  ['id'] + [*form.base_fields] 
        # calculated_fields = [field.name for field in self.model._meta.fields if field.name.startswith('calculated_')]
        # calculated_fields = [name for name in calculated_fields if not name.endswith('_currency')]

        all_fields = [field.name for field in self.model._meta.fields if not field.name.endswith('_currency')]

        return [field for field in all_fields if field not in hidden_fields]
    

    
    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:

        non_editable_fields = [field.name for field in self.model._meta.fields if not field.editable]
        non_editable_fields = [name for name in non_editable_fields if not name.endswith('_currency')]
        readonly_fields = non_editable_fields

        return readonly_fields
    


    def get_list_display_fields(self, request: HttpRequest | None = None, obj: Any = None) -> list[str]:
        excluded_names = ['created_at', 'created_by', 'updated_at', 'updated_by', 'notes',  'unit_price_currency', 'total_brokerage_currency', '_creation_handled']
        if portfolio_scope.is_scoped(self.model):
            excluded_names.append('account')  # Every row is the selected portfolio's.
        fields = [
            field.name
            for field in self.model._meta.get_fields()
            if not (field.many_to_many or field.one_to_many or field.one_to_one)
            and field.name not in excluded_names
        ]
        return fields
        
    def get_list_filter_fields(self, request: HttpRequest | None = None, obj: Any = None) -> list[str]:
        filterable_fields = ['instrument'] if portfolio_scope.is_scoped(self.model) else ['instrument', 'account']
        return [field.name for field in self.model._meta.get_fields() if field.name in filterable_fields]
    
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # A record that knows why it cannot be deleted (a share split whose parcels have
        # since been sold) hides the button rather than failing on the confirmation page.
        blocker = getattr(obj, 'deletion_blocker', None)
        if obj is not None and blocker is not None and blocker():
            return False
        return super().has_delete_permission(request, obj)


    #: Approximate total form fields allowed across a change page's inlines, so saving does not
    #: hit Django's TooManyFieldsSent limit.
    INLINE_FIELD_BUDGET = 6000

    def get_inline_instances(self, request: HttpRequest, obj: Any = None) -> list[InlineModelAdmin]:

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



class GenericModelAdminWithoutAdd(GenericModelAdmin):
    """For records derived from others (parcels), which are neither added nor deleted by hand.

    Deleting a parcel would remove a holding while the buy that created it remained.
    """
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


#: Derived records a delete may take with the record they came from, though not on their own.
DELETED_WITH_THEIR_SOURCE = {str(Parcel._meta.verbose_name)}


class HiddenModelAdmin(admin.ModelAdmin):
    search_fields = ('id', 'description')
    def has_module_permission(self, request: HttpRequest) -> bool:
        return False  # hides from sidebar



class AppUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = AppUser

class AppUserAdmin(UserAdmin):
    form = AppUserChangeForm

    fieldsets = UserAdmin.fieldsets + (
            (None, {'fields': ('default_account',)}),
    )  # type: ignore[operator]

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        """The selected portfolio can only be one the user being edited may see."""
        form = super().get_form(request, obj, change=change, **kwargs)
        if 'default_account' in form.base_fields:
            field: Any = form.base_fields['default_account']
            field.queryset = obj.portfolios() if obj is not None else Account.objects.none()
        return form


class AccountAdmin(admin.ModelAdmin):
    """Portfolios: a superuser's are all of them, anyone else's their own."""

    search_fields = ('id', 'description')

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        user: Any = request.user
        portfolios: QuerySet[Any] = user.portfolios()
        return portfolios

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        user: Any = getattr(request, 'user', None)
        if db_field.name == 'owner' and user is not None:
            kwargs['initial'] = user.pk
            if not user.is_superuser:
                kwargs['queryset'] = AppUser.objects.filter(pk=user.pk)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def response_add(self, request: HttpRequest, obj: Any, post_url_continue: str | None = None) -> Any:
        """A portfolio added here is selected, so it is the one the dashboard and lists show."""
        portfolio_scope.select(request, obj)
        messages.info(request, f'Selected {obj.description}, so the dashboard and lists show it.')
        return super().response_add(request, obj, post_url_continue)


class SharedTableAdmin(GenericModelAdmin):
    """Reference data every portfolio uses (fiscal years, CPI), so only a superuser changes it."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return request.user.is_superuser and super().has_delete_permission(request, obj)


class ExchangeRateAdmin(GenericModelAdmin):
    """Rates come from the market data provider; a wrong or stand-in one is corrected here."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        # The account is not editable, so a rate added here could never be saved. A missing
        # rate is fetched when a record needs it, and a stand-in is corrected in place.
        return False

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        corrected = change and 'exchange_rate_multiplier' in form.changed_data
        super().save_model(request, obj, form, change)
        if corrected:
            obj.rate_corrected()



CONFIRM_LEGAL_FORM_FIELD = 'confirm_legal_form'


class UnsetNullBooleanSelect(forms.NullBooleanSelect):
    """NullBooleanSelect for the TAP override, labelled by what each option does.

    "No" overrides the s104-165(3) departure deeming, which the plain label hides. Only the
    labels change; the submitted values are the standard ones.
    """

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs)
        self.choices = [
            ('unknown', 'Unset - derive it per parcel'),
            ('true', 'Yes - always taxable Australian property'),
            ('false', 'No - never, overriding the departure deeming'),
        ]


class InstrumentAdminForm(forms.ModelForm):
    """Instrument form with a "Confirm legal form" tick, to accept a suggestion unchanged.

    An explicit tick, so saving an unrelated field never confirms the legal form.
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

    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:  # type: ignore[override]
        """Show the confirm tick only for a saved, unconfirmed instrument with a legal form."""
        fields = list(super().get_fields(request, obj))
        if obj is None or obj.is_classified or obj.legal_form == LegalForm.UNKNOWN:
            return [name for name in fields if name != CONFIRM_LEGAL_FORM_FIELD]
        if CONFIRM_LEGAL_FORM_FIELD not in fields:
            position = (fields.index('legal_form_source') + 1
                        if 'legal_form_source' in fields else len(fields))
            fields.insert(position, CONFIRM_LEGAL_FORM_FIELD)
        return fields

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
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
    def confirm_legal_form_action(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        """Confirm the selected instruments' legal forms, naming any with none set."""
        confirmed = 0
        already = 0
        unclassified: list[str] = []

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


class CostBaseAdjustmentAdmin(GenericModelAdmin):
    """Cost base adjustments, plus a way to spread one again over the holding as it now stands."""

    actions = ['reallocate_action']

    @admin.action(description='Reallocate over the holding as it now stands')
    def reallocate_action(self, request: HttpRequest, queryset: QuerySet[Any]) -> TemplateResponse | None:
        """Show what reallocating would change, then reallocate once confirmed."""
        from share_dinkum_app.choices import AllocationMethod
        from share_dinkum_app.holdings import live

        spread = [a for a in queryset.select_related('instrument')
                  if a.allocation_method == AllocationMethod.QTY_HELD]
        by_instrument: dict[Any, list[Any]] = {}
        for adjustment in spread:
            by_instrument.setdefault(adjustment.instrument, []).append(adjustment.pk)

        if request.POST.get('confirm') != 'yes':
            reallocations = [
                reallocation
                for instrument, ids in by_instrument.items()
                for reallocation in live.preview_reallocation(instrument, ids)]
            return TemplateResponse(request, 'admin/reallocate_confirmation.html', {
                **self.admin_site.each_context(request),
                'opts': self.model._meta,
                'title': 'Reallocate cost base adjustments',
                'reallocations': reallocations,
                'skipped': [str(a) for a in queryset if a.allocation_method != AllocationMethod.QTY_HELD],
                'selected': [str(pk) for pk in queryset.values_list('pk', flat=True)],
                'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            })

        for instrument, ids in by_instrument.items():
            try:
                with transaction.atomic():
                    result = live.reallocate(instrument, ids)
            except live.HoldingsRefused as refusal:
                messages.error(request, str(refusal))
                continue
            if result.empty:
                messages.info(request, f'{instrument.name}: already divided as the holding stands.')
            else:
                messages.success(request, f'{instrument.name}: reallocated {len(ids)} adjustment(s).')
        return None


# Map specific models to custom admin if required, or hide them.

model_admin_map: "dict[type[Model], type[admin.ModelAdmin[Any]]]" = {

    Account : AccountAdmin,
    CostBaseAdjustment : CostBaseAdjustmentAdmin,
    ExchangeRate : ExchangeRateAdmin,
    Instrument : InstrumentAdmin,
    AppUser : AppUserAdmin,
    CPIIndex : SharedTableAdmin,
    FiscalYear : SharedTableAdmin,
    FiscalYearType : SharedTableAdmin,
    Group : HiddenModelAdmin,
    ContentType : HiddenModelAdmin,
    Parcel : GenericModelAdminWithoutAdd,

}

try:
    admin.site.unregister(Group)
except NotRegistered:
    pass


for model in apps.get_app_config('share_dinkum_app').get_models():

    model_admin_map.setdefault(model, GenericModelAdmin)

for model, model_admin in model_admin_map.items():
    try:
        if model_admin and issubclass(model, Model):
            admin.site.register(model, model_admin)
    except AlreadyRegistered:
        logger.error(f'Failed to register {model} with {model_admin}. Already registered?')



without_add = ['Parcel']

