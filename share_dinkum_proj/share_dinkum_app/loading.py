
from xml.parsers.expat import model
import pandas as pd

from collections import defaultdict, deque


from djmoney.money import Money

from share_dinkum_app import excelinterface
from share_dinkum_app import yfinanceinterface


from django.apps import apps
import django

import share_dinkum_app.models as app_models

from datetime import date, timedelta

from django.db.models import ForeignKey, OneToOneField, DecimalField, FileField, DateField
from django.core.exceptions import ObjectDoesNotExist

from django.core.files.base import ContentFile

from django.db import transaction
from django.conf import settings


from share_dinkum_app.utils import convert_to_decimal, save_with_logging, process_filefield


import share_dinkum_app
import shutil

from tqdm import tqdm

from pathlib import Path


from share_dinkum_app.utils.signal_helpers import disconnect_app_signals, reconnect_app_signals

import logging
logger = logging.getLogger(__name__)


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
                        record[field_name + '__name'] = getattr(field_value, 'name')
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

    @classmethod
    def get_model_load_order(cls):

        model_load_order = {
            
            'AppUser': share_dinkum_app.models.AppUser,
            'FiscalYearType': share_dinkum_app.models.FiscalYearType,
            'FiscalYear': share_dinkum_app.models.FiscalYear,
            'Account': share_dinkum_app.models.Account,
            'LogEntry': share_dinkum_app.models.LogEntry,
            'ExchangeRate': share_dinkum_app.models.ExchangeRate,
            'Market': share_dinkum_app.models.Market,
            'Instrument': share_dinkum_app.models.Instrument,
            'InstrumentPriceHistory': share_dinkum_app.models.InstrumentPriceHistory,
            'Buy': share_dinkum_app.models.Buy,
            'Sell': share_dinkum_app.models.Sell,
            'Parcel': share_dinkum_app.models.Parcel,
            'SellAllocation': share_dinkum_app.models.SellAllocation,
            'ShareSplit': share_dinkum_app.models.ShareSplit,
            'CostBaseAdjustment': share_dinkum_app.models.CostBaseAdjustment,
            'CostBaseAdjustmentAllocation': share_dinkum_app.models.CostBaseAdjustmentAllocation,
            'Dividend': share_dinkum_app.models.Dividend,
            'Distribution': share_dinkum_app.models.Distribution,
            'DataExport': share_dinkum_app.models.DataExport
        }

        return model_load_order.values()
    


        # models = {model.__name__: model for model in apps.get_models() if model._meta.app_label == 'share_dinkum_app'}
        # model_load_order = # TODO: Define the correct order of models based on dependencies




    def load_all_tables(self):

        model_load_order = self.get_model_load_order()

        for model in model_load_order:
            table_name = model.__name__

            if table_name in ['LogEntry']:
                continue  # Skip loading LogEntry as ContentType as a name property, not field. Hard to loookup by name.

            df = self.mapping.get(table_name)
            if df is not None:
                logger.info(f"Loading {table_name}")
                self.load_table_to_model(model=model, df=df)


    def load_table_to_model(self, model, df):

        df = df.copy()
        
        # Legacy data import template has a column 'copy_from_path' which is used to load files.
        # Now, can just use 'file' as the column name, so the export template can be used for importing data also.
        df = df.rename(columns={'copy_from_path': 'file'}, errors='ignore')

        cols_to_drop = ['created_at', 'updated_at', '_creation_handled']

        cols_to_drop += [col for col in df.columns if col.startswith('calculated_')]

        df = df.drop(columns=cols_to_drop, errors='ignore')

        if 'account' in [f.name for f in model._meta.fields]:
            df['account_id'] = self.account.id

        if 'is_active' in df.columns:
            df['is_active'] = df['is_active'].fillna(True)
        
        # Preprocess columns to handle foreign keys,  decimal fields, and file fields.
        for col in df.columns:

            # Lookup fields are not processed here.
            if col.startswith('lookup_'):
                continue
            
            # Foreign key lookup by name
            col_parts = col.split('__')   # eg 'instrument__name' > ['instrument', 'name']
            if len(col_parts) == 2: 
                base_field_name = col_parts[0]   # instrument
                lookup_field = col_parts[1]  # i.e. name
                field_instance = model._meta.get_field(base_field_name)
                related_model = field_instance.related_model


                df[base_field_name] = df[col].apply(
                    lambda field_val : self.get_related_obj_by_name(
                        related_model=related_model, 
                        account=self.account,
                        filters={lookup_field : field_val}
                        ) if field_val else None
                            )
                df = df.drop(columns=[col])
                continue


            field_instance = model._meta.get_field(col)

            if isinstance(field_instance, DecimalField):
                max_digits = field_instance.max_digits
                decimal_places = field_instance.decimal_places
                convert_to_decimal_to_apply = lambda value: convert_to_decimal(value, max_digits=max_digits, decimal_places=decimal_places)
                df[col] = df[col].apply(convert_to_decimal_to_apply)
            
            elif isinstance(field_instance, FileField):
                df[col] = df[col].apply(process_filefield)


        # Change any NaT, NaN etc to None
        df = df.where(pd.notnull(df), None)


        for index, row in tqdm(df.iterrows(), total=len(df)):

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


            # This is used on loading sell allocations using legacy id.
            lookup_legacy_sell = record.pop('lookup_legacy_sell', None)
            if lookup_legacy_sell:
                sell = self.get_related_obj_by_name(related_model=app_models.Sell, account=self.account, filters={'legacy_id' : lookup_legacy_sell})
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
                    logger.error(f"Error looking up legacy buy id {lookup_legacy_buy} for model {model.__name__}: {e}", exc_info=True)
                    logger.error('Error on row:\n', row)
                    raise e


            if id:
                # Try to update, otherwise create
                try:
                    obj = model.objects.get(id=id)
                    for field, value in record.items():
                        setattr(obj, field, value)
                    save_with_logging(obj=obj, context="Updating existing object")
                    obj.save()
                
                except ObjectDoesNotExist:
                    # Object with ID does not exist; create new
                    record['id'] = id  # Preserve provided ID
                    obj = model(**record)
                    save_with_logging(obj=obj, context="Creating new object with explicitly provided ID")
            else:
                obj = model(**record)
                save_with_logging(obj=obj, context="Creating new object without provided ID")




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
        available_parcels = [parcel for parcel in available_parcels if parcel.remaining_quantity > 0]
        return available_parcels


    def get_related_obj_by_name(self, related_model, account, filters):

        if not filters:
            return None

        # Get all field names of the related model
        related_model_fields = {f.name for f in related_model._meta.get_fields()}

        # Add 'account' to filters only if it exists on the related model
        if 'account' in related_model_fields:
            filters['account'] = account

        try:
            return related_model.objects.get(**filters)
        except related_model.DoesNotExist:
            logger.error(f"No match found for {related_model.__name__} with filters: {filters}")
            raise
        except related_model.MultipleObjectsReturned:
            logger.error(f"Multiple matches found for {related_model.__name__} with filters: {filters}")
            raise


    @classmethod
    def clear_all_data(cls):
        res = input("Type 'X' to DELETE ALL DATA.")
        if res.upper() != 'X':
            logger.info('Aborted')
            return
        # Clear database tables

        model_load_order = cls.get_model_load_order()

        model_deletion_order = [model for model in apps.get_models() if model not in model_load_order] + list(reversed(model_load_order))

        receivers = disconnect_app_signals('share_dinkum_app')

        for model in model_deletion_order:
            try:
                model.objects.all().delete()  # Deletes all records in the model
            except Exception as e:
                logger.error(f"Error deleting model {model.__name__}: {e}", exc_info=True)
                raise
 
        
        reconnect_app_signals(receivers)


        logger.info('Deleted all models')
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
                logger.error(f"Failed to delete {item}: {e}", exc_info=True)
    # Recreate folder
    folder.mkdir(parents=True, exist_ok=True)
    logger.info(f"Forcefully deleted and recreated folder: {folder}")
    
