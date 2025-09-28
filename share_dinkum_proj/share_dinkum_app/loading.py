import pandas as pd

from datetime import date, datetime
import shutil
from tqdm import tqdm
from pathlib import Path

from django.apps import apps
from django.db.models import DecimalField, FileField
from django.db import connections, transaction
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings

from djmoney.money import Money

import share_dinkum_app
from share_dinkum_app import excelinterface
from share_dinkum_app import yfinanceinterface
import share_dinkum_app.models as app_models
from share_dinkum_app.utils import convert_to_decimal, save_with_logging, process_filefield
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

        # TODO work out the ordering based on the model dependencies
        model_load_order = {
            
            'AppUser': share_dinkum_app.models.AppUser,
            'FiscalYearType': share_dinkum_app.models.FiscalYearType,
            'FiscalYear': share_dinkum_app.models.FiscalYear,
            'Account': share_dinkum_app.models.Account,
            'LogEntry': share_dinkum_app.models.LogEntry,
            'CurrentExchangeRate': share_dinkum_app.models.CurrentExchangeRate,
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
        
        logger.debug('Starting to process columns')
        # Preprocess columns to handle foreign keys,  decimal fields, and file fields.
        for col in df.columns:
            logger.debug('Starting to process columns %s', col)
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





from share_dinkum_app.models import Account, DataExport

class DataBackupManager:

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)


    def create_data_exports_for_all_accounts(self, include_price_history: bool = True):
        """
        Create a DataExport for each Account.
        The signals attached to DataExport will generate the files automatically.
        """
        accounts = Account.objects.all()
        logger.info(f"Creating DataExport for {accounts.count()} accounts")
        with transaction.atomic():
            for account in accounts:
                export = DataExport.objects.create(
                    account=account,
                    include_price_history=include_price_history
                )
                export.refresh_from_db()


    def cleanup_old_backups(self, name, keep=5):
        """
        Keep only the most recent 'keep' backups in the specified backup folder.
        """
        logger.info(f"Cleaning up old backups in {name}, keeping the most recent {keep} backups.")
        backup_base_path = self.base_path / name
        if not backup_base_path.exists():
            logger.info(f"No backups found in {backup_base_path} to clean up.")
            return

        backups = [folder for folder in backup_base_path.iterdir() if folder.is_dir()]
        backups_sorted = sorted(backups, key=lambda x: x.name, reverse=True)

        old_backups = backups_sorted[keep:]
        for old_backup in old_backups:
            try:
                shutil.rmtree(old_backup)
                logger.info(f"Deleted old backup: {old_backup}")
            except Exception as e:
                logger.error(f"Failed to delete old backup {old_backup}: {e}", exc_info=True)



    def backup(self, name):
        """
        Backup SQLite DB, media folder, and create DataExport files for all accounts.
        """
        folder_name = datetime.now().strftime("%Y-%m-%dT%H%M")
        backup_path = self.base_path / name / folder_name

        backup_path.mkdir(parents=True, exist_ok=False)

        # Create DataExports for all accounts
        self.create_data_exports_for_all_accounts()

        # Backup SQLite DB
        db_file = Path(settings.DATABASES['default']['NAME'])
        backup_db_file = backup_path / db_file.name
        logger.info(f"Backing up SQLite DB from {db_file} to {backup_db_file}")
        shutil.copy2(db_file, backup_db_file)

        # Backup media folder (includes DataExport files)
        media_backup = backup_path / "media"
        if media_backup.exists():
            shutil.rmtree(media_backup)
        shutil.copytree(Path(settings.MEDIA_ROOT), media_backup)

        self.cleanup_old_backups(name=name, keep=5)

        logger.info(f"Backup completed successfully at {backup_path}")

    def restore(self, name):
        """
        Restore SQLite DB and media folder from backup.
        """

        backup_base_path = self.base_path / name
 
        backups = [folder.name for folder in backup_base_path.iterdir() if folder.is_dir()]
        backups_sorted = sorted(backups, reverse=True)
        backups_sorted = backups_sorted[:5]  # Show only the 5 most recent backups
        if not backups_sorted:
            logger.error(f"No backups found in {backup_base_path}")
            return
        backup_choice_text = "\n".join([f"{i+1}. {backup[:10]}" for i, backup in enumerate(backups_sorted)])

        choice = input(f"Available backups:\n{backup_choice_text}\nSelect a backup to restore (1-{len(backups_sorted)}). Type '1' to choose the latest backup.\n:")
        try:
            choice_index = int(choice) - 1
            if choice_index < 0 or choice_index >= len(backups_sorted):
                raise ValueError("Choice out of range")
            selected_backup = backups[choice_index]
        except Exception as e:
            logger.error(f"Invalid choice. Restore cancelled.")
            return

        backup_path = backup_base_path / selected_backup

        res = input("Type 'X' to OVERWRITE current data with backup.")
        if res.upper() != 'X':
            logger.info("Restore cancelled.")
            return
        
        logger.info(f"Restoring from backup: {backup_path}")

        db_file = Path(settings.DATABASES['default']['NAME'])
        backup_db_file = backup_path / db_file.name
        media_backup = backup_path / "media"

        if not backup_db_file.exists() or not media_backup.exists():
            raise FileNotFoundError("Backup is incomplete or missing files")

        # Close DB connections
        connections.close_all()

        # Restore DB
        logger.info(f"Restoring SQLite DB from {backup_db_file} to {db_file}")
        shutil.copy2(backup_db_file, db_file)

        # Restore media
        if Path(settings.MEDIA_ROOT).exists():
            shutil.rmtree(Path(settings.MEDIA_ROOT))
        shutil.copytree(media_backup, Path(settings.MEDIA_ROOT))

        logger.info("Restore completed successfully.")