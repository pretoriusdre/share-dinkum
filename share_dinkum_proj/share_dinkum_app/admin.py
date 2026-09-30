from django.contrib import admin, messages
from django import forms

from django.apps import apps
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, ForeignKey, ManyToManyRel

import share_dinkum_app
import share_dinkum_app.models

from share_dinkum_app.choices import LegalForm, LegalFormSource
from share_dinkum_app.models import (
    AppUser,
    Account,
    ExchangeRate,
    Parcel,
    Instrument,
)

import logging

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
    
    def has_delete_permission(self, request, obj=None):
        # A record that knows why it cannot be deleted (a share split whose parcels have
        # since been sold) hides the button rather than failing on the confirmation page.
        blocker = getattr(obj, 'deletion_blocker', None)
        if obj is not None and blocker is not None and blocker():
            return False
        return super().has_delete_permission(request, obj)


    #: Approximate total form fields allowed across a change page's inlines, so saving does not
    #: hit Django's TooManyFieldsSent limit.
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
    """For records derived from others (parcels), which are neither added nor deleted by hand.

    Deleting a parcel would remove a holding while the buy that created it remained.
    """
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


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


class ExchangeRateAdmin(GenericModelAdmin):
    """Rates come from the market data provider; a wrong or stand-in one is corrected here."""

    def has_add_permission(self, request):
        # The account is not editable, so a rate added here could never be saved. A missing
        # rate is fetched when a record needs it, and a stand-in is corrected in place.
        return False

    def save_model(self, request, obj, form, change):
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

    def __init__(self, attrs=None):
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

    def get_fields(self, request, obj=None):
        """Show the confirm tick only for a saved, unconfirmed instrument with a legal form."""
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
        """Confirm the selected instruments' legal forms, naming any with none set."""
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
    ExchangeRate : ExchangeRateAdmin,
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

