
import pandas as pd
from djmoney.money import Money

from share_dinkum_app import excelinterface
from share_dinkum_app import yfinanceinterface


from django.apps import apps
import django

import share_dinkum_app.models as app_models

from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from django.db.models import ForeignKey, OneToOneField, DecimalField
from django.core.exceptions import ObjectDoesNotExist

from django.core.files.base import ContentFile

from django.db import transaction
from django.conf import settings


import shutil

from tqdm import tqdm

from pathlib import Path



def make_tz_naive(df):
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.tz_localize(None)
    return df


def model_to_queryset(model, account=None):
    fields = [f.name for f in model._meta.fields]
    related_fields = [f.name for f in model._meta.fields if f.is_relation]
    queryset = model.objects.select_related(*related_fields).all()
    if account:
        queryset = queryset.filter(account=account)
    return queryset


def queryset_to_df(queryset):

    model = queryset.model

    fields = [f.name for f in model._meta.fields]
    related_fields = [f.name for f in model._meta.fields if f.is_relation]

    # Include model properties (calculated fields)
    properties = [attr for attr in dir(model) if isinstance(getattr(model, attr), property)]


    data = []
    for obj in queryset:
        record = {}
        for field_name in fields:
            field_value = getattr(obj, field_name)
            if field_name in related_fields:
                # Get the related object's 'name' attribute if the field is a related field
                if field_value is not None:
                    if hasattr(field_value, 'name'):
                        record[field_name + '_name'] = getattr(field_value, 'name')
                    else:
                        record[field_name + '_id'] = field_value.id
            else:
                record[field_name] = field_value

        # Include calculated fields (properties) with 'calculated_' prefix
        # for prop_name in properties:
        #     if prop_name not in ['pk', 'associated_logs']:
        #         calc_field_value = getattr(obj, prop_name)
        #         record[f'calc_{prop_name}'] = calc_field_value
        #         if isinstance(calc_field_value, Money):
        #             record[f'calc_{prop_name}_currency'] = str(calc_field_value.currency)


        data.append(record)

    df = pd.DataFrame(data)
    df = make_tz_naive(df)

    hidden_columns = ['password']
    for column in hidden_columns:
        if column in df.columns:
            df = df.drop(columns=column)

    return df



class DataLoader():

    def __init__(self, account, input_file=None):

        self.input_file = input_file
        self.account = account

        if self.input_file:
            self.mapping = excelinterface.get_all_tables_in_excel(self.input_file)
            self.load_all_tables()




    def load_all_tables(self):

        loadable_models = {}
        excluded_models = ['AppUser', 'AppUser_groups', 'AppUser_user_permissions', 'Account', 'FiscalYearType', 'ExchangeRate', 'DataExport', 'LogEntry', 'DataImport', 'Parcel']
        for model_name in apps.all_models['share_dinkum_app']:
            model = django.apps.apps.get_model('share_dinkum_app', model_name)
            if model.__name__ not in excluded_models:
                loadable_models[model.__name__] = model

        for table_name, df in self.mapping.items():
            model = loadable_models.get(table_name)
            if model:
                print(f'Starting to load data to {model}')
                self.load_table_to_model(model=model, df=df)


    def load_table_to_model(self, model, df):

        df = df.copy()
        
        for col in df.columns:
            col_parts = col.split('__')   # eg 'instrument_name' > ['instrument', 'name']

            if len(col_parts) == 2: 
                base_field_name = col_parts[0]   # instrument
                lookup_field = col_parts[1]  # name
                field_instance = model._meta.get_field(base_field_name)
                related_model = field_instance.related_model
                df[base_field_name] = df[col].apply(lambda field_val : self.get_related_obj_by_name(related_model=related_model, filters={'account' : self.account, lookup_field : field_val}) if field_val else None)
                df = df.drop(columns=[col])

            elif col.startswith('calculated_'):
                df = df.drop(columns=[col])

            elif not col.startswith('lookup_') and col != 'copy_from_path': # Need to exclude lookup fields

                field_instance = model._meta.get_field(col)
                if isinstance(field_instance, DecimalField):
                    max_digits = field_instance.max_digits
                    decimal_places = field_instance.decimal_places
                    def convert_to_decimal(value):
                        if value is None:
                            return None
                        try:
                            decimal_value = Decimal(value)
                            quantizer = Decimal('1.' + '0' * decimal_places)
                            decimal_value = decimal_value.quantize(quantizer, rounding=ROUND_DOWN)
                            if len(str(decimal_value).replace('.', '').replace('-', '')) > max_digits:
                                raise ValueError(f"Value {value} exceeds max_digits ({max_digits}) for field {col}")
                            
                            return decimal_value
                        except Exception as e:
                            raise ValueError(f"Error converting value {value} in column '{col}' to Decimal: {e}")
                    df[col] = df[col].apply(convert_to_decimal)

        for index, row in tqdm(df.iterrows()):

            record = dict(row)
            record['account_id'] = self.account.id
            
            id = record.pop('id', None)

            # exchange_rate = None
            # for field, val in record.items():
            #     if not exchange_rate and field.endswith('_currency') and val != self.account.currency:
                    
            #         # TODO remove exchange_rate = self.get_or_create_exchange_rate(convert_from=val, exchange_date=record['date'])
            #         exchange_rate = None
                    

            # if exchange_rate:
            #     record['exchange_rate'] = exchange_rate

            copy_from_path = record.pop('copy_from_path', None)

            if copy_from_path:
                file_path = Path(copy_from_path)
                if file_path.exists():
                    with open(file_path, 'rb') as f:
                        file_content = ContentFile(f.read(), name=file_path.name)
                        record['file'] = file_content
                else:
                    print(f'File path {file_path} does not exist')


            # This is used on loading sell allocations using legacy id.
            lookup_legacy_sell = record.pop('lookup_legacy_sell', None)
            if lookup_legacy_sell:
                sell = self.get_related_obj_by_name(related_model=app_models.Sell, filters={'account' : self.account, 'legacy_id' : lookup_legacy_sell})
                record['sell'] = sell

            # This is used for loading buy allocations using legacy buy id.
            lookup_legacy_buy = record.pop('lookup_legacy_buy', None)
            if lookup_legacy_buy:
                try:
                    available_parcels = self.get_available_parcels(legacy_id=lookup_legacy_buy)
                    assert len(available_parcels) == 1
                    parcel = available_parcels[0]
                    record['parcel'] = parcel
                except Exception as e:
                    print(e)
                    print('Error', row)
                    raise e

            if id:
                # If the object exists, update it
                try:
                    obj = model.objects.get(id=id)

                    for field, value in record.items():
                        if field != 'id':
                            setattr(obj, field, value)
                    obj.save()

                except ObjectDoesNotExist as e:
                    print(f"Record with id {id} does not exist in {model}.")
                    raise e

            else:
                # Create new object.
                try:
                    obj = model(**record)
                    obj.save()

                except Exception as e:
                    print(e)
                    print(record)
                    raise e



    def get_or_create_exchange_rate(self, convert_from, exchange_date):
        convert_to = self.account.currency
        if convert_from == convert_to:
            return None
        
        exchange_rate_multiplier = yfinanceinterface.get_exchange_rate(convert_from=convert_from, convert_to=convert_to, exchange_date=exchange_date)
        record = {
            'account' : self.account,
            'date' : date.fromisoformat(str(exchange_date)),
            'convert_from' : convert_from,
            'convert_to' : convert_to,
            'exchange_rate_multiplier' : exchange_rate_multiplier
            }
        exchange_rate, created = app_models.ExchangeRate.objects.get_or_create(**{'convert_from': convert_from, 'convert_to' : convert_to, 'date' : exchange_date}, defaults=record)
        return exchange_rate


    def get_available_parcels(self, legacy_id):
        available_parcels = app_models.Parcel.objects.filter(account=self.account, buy__legacy_id=legacy_id, deactivation_date__isnull=True)
        available_parcels = [parcel for parcel in available_parcels if parcel.unsold_quantity > 0]
        return available_parcels



    def get_related_obj_by_name(self, related_model, filters):
        if filters:
            obj = related_model.objects.get(**filters)
            return obj
        return None







def clear_all_data():
    res = input("Type 'X' to DELETE ALL DATA.")
    if res.upper() != 'X':
        print('Aborted')
        return
    # Clear database tables
    all_app_models = apps.get_models()
    for model in reversed(list(all_app_models)):
        try:
            model.objects.all().delete()  # Deletes all records in the model
        except Exception as e:
            print(f"Error deleting model {model.__name__}: {e}")
    print('Deleted all models')
    # Delete all data in the media folder
    media_folder = Path(settings.MEDIA_ROOT)
    force_delete_and_recreate_folder(media_folder)
    



def force_delete_and_recreate_folder(folder_path):
    folder = Path(folder_path)
    # Check if folder exists
    if folder.exists():
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()  # Force delete files
            except Exception as e:
                print(f"Failed to delete {item}: {e}")
    # Recreate folder
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Forcefully deleted and recreated folder: {folder}")
    
