from django.contrib import admin


#import share_dinkum_app.models

from django.apps import apps

from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm

from django.db import models
from django.db.models import Model, ForeignKey
from django.db.models.fields.reverse_related import ManyToManyRel

#from django.contrib.auth.models import Group

from django.db.models import ManyToManyRel, ManyToManyField



import share_dinkum_app
import share_dinkum_app.admin
import share_dinkum_app.models

from share_dinkum_app.models import AppUser


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


from share_dinkum_app.models import Account, Parcel

# Map specific models to custom admin if required, or hide them.
model_admin_map = {
    Account : AccountAdmin,
    AppUser : AppUserAdmin,
    #Buy : admin.ModelAdmin
    #Group : None
    Parcel : GenericModelAdminWithoutAdd
}


from share_dinkum_app.models import Buy




from django.contrib.contenttypes.models import ContentType

admin.site.register(ContentType, GenericModelAdmin)


for model in apps.get_app_config('share_dinkum_app').get_models():

    model_admin_map.setdefault(model, GenericModelAdmin)

for model, model_admin in model_admin_map.items():
    try:
        if model_admin and issubclass(model, Model):
            admin.site.register(model, model_admin)
    except admin.sites.AlreadyRegistered:
        logger.error(f'Failed to register {model} with {model_admin}. Already registered?')



without_add = ['Parcel']

