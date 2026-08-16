import pandas as pd

from datetime import date, datetime
import shutil
import sqlite3
from tqdm import tqdm
from pathlib import Path

from django.apps import apps
from django.db.models import DecimalField, FileField
from django.db import connections, transaction
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.core.management import call_command


from djmoney.money import Money

import share_dinkum_app
from share_dinkum_app import excelinterface
from share_dinkum_app import yfinanceinterface
import share_dinkum_app.models as app_models
from share_dinkum_app.utils import convert_to_decimal_field, save_with_logging, process_filefield
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

        # Normalise pandas null sentinels (NaN, NaT, pd.NA) before field processing
        df = df.astype(object).where(pd.notna(df), None)
        
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
                df[col] = df[col].apply(lambda v: convert_to_decimal_field(v, field_instance))
            

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

        call_command('flush', interactive=False)

        logger.info('Deleted all models and reset DataLoader state.')
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

    # Seconds are included so two backups taken in the same minute do not collide. Sorting stays
    # chronological against older folders created before seconds were added, because those names
    # are a prefix of the longer ones.
    BACKUP_FOLDER_FORMAT = "%Y-%m-%dT%H%M%S"

    RETAIN_BACKUPS = 5

    # Restores copy the live data here first, so an unwanted restore can be undone.
    PRE_RESTORE_NAME = 'pre_restore'
    RETAIN_PRE_RESTORE = 3

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)


    def list_backups(self, name):
        """Backup folder names within a set, newest first.

        Every caller orders backups through this one method. Listing and selecting from separate
        orderings is how a restore ends up loading a different backup from the one displayed.
        """
        backup_base_path = self.base_path / name
        if not backup_base_path.exists():
            return []

        return sorted(
            (folder.name for folder in backup_base_path.iterdir() if folder.is_dir()),
            reverse=True,
        )


    @staticmethod
    def copy_sqlite_database(source: Path, destination: Path):
        """Copy a SQLite database using its online backup API.

        A plain file copy of a database that is being written to can capture a torn file, and the
        development server may well be running while a backup is taken. The backup API takes a
        consistent snapshot regardless.
        """
        source_connection = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
        try:
            destination_connection = sqlite3.connect(destination)
            try:
                with destination_connection:
                    source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()


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

        for folder_name in self.list_backups(name)[keep:]:
            old_backup = backup_base_path / folder_name
            try:
                shutil.rmtree(old_backup)
                logger.info(f"Deleted old backup: {old_backup}")
            except Exception as e:
                logger.error(f"Failed to delete old backup {old_backup}: {e}", exc_info=True)



    def backup(self, name):
        """
        Backup SQLite DB, media folder, and create DataExport files for all accounts.
        """
        folder_name = datetime.now().strftime(self.BACKUP_FOLDER_FORMAT)
        backup_path = self.base_path / name / folder_name

        backup_path.mkdir(parents=True, exist_ok=False)

        # Create DataExports for all accounts
        self.create_data_exports_for_all_accounts()

        # Backup SQLite DB
        db_file = Path(settings.DATABASES['default']['NAME'])
        backup_db_file = backup_path / db_file.name
        logger.info(f"Backing up SQLite DB from {db_file} to {backup_db_file}")
        self.copy_sqlite_database(db_file, backup_db_file)

        # Backup media folder (includes DataExport files)
        media_backup = backup_path / "media"
        shutil.copytree(Path(settings.MEDIA_ROOT), media_backup)

        self.cleanup_old_backups(name=name, keep=self.RETAIN_BACKUPS)

        logger.info(f"Backup completed successfully at {backup_path}")

    def restore(self, name):
        """
        Restore SQLite DB and media folder from backup.
        """

        backup_base_path = self.base_path / name

        # The menu and the selection must index the same list, or the restore loads a different
        # backup from the one shown.
        recent_backups = self.list_backups(name)[:5]
        if not recent_backups:
            logger.error(f"No backups found in {backup_base_path}")
            return

        backup_choice_text = "\n".join(
            f"{i + 1}. {backup}{'   (latest)' if i == 0 else ''}"
            for i, backup in enumerate(recent_backups)
        )

        choice = input(f"Available backups:\n{backup_choice_text}\nSelect a backup to restore (1-{len(recent_backups)}). Type '1' to choose the latest backup.\n:")
        try:
            choice_index = int(choice) - 1
            if choice_index < 0 or choice_index >= len(recent_backups):
                raise ValueError("Choice out of range")
            selected_backup = recent_backups[choice_index]
        except Exception as e:
            logger.error(f"Invalid choice. Restore cancelled.")
            return

        backup_path = backup_base_path / selected_backup

        db_file = Path(settings.DATABASES['default']['NAME'])
        backup_db_file = backup_path / db_file.name
        media_backup = backup_path / "media"

        # Validate before asking to confirm, so an unusable backup cannot destroy the live data.
        if not backup_db_file.exists() or not media_backup.exists():
            raise FileNotFoundError(f"Backup {selected_backup} is incomplete or missing files")

        res = input(f"Type 'X' to OVERWRITE current data with the backup taken at {selected_backup}.")
        if res.upper() != 'X':
            logger.info("Restore cancelled.")
            return

        logger.info(f"Restoring from backup: {backup_path}")

        self.snapshot_current_data()

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


    def snapshot_current_data(self):
        """Copy the live database and media aside so that a restore can be undone.

        Deliberately does not create DataExport records the way backup() does: this runs on data
        that is about to be replaced, and should not write to the database it is preserving.
        Recover with restore(name=DataBackupManager.PRE_RESTORE_NAME).
        """
        folder_name = datetime.now().strftime(self.BACKUP_FOLDER_FORMAT)
        snapshot_path = self.base_path / self.PRE_RESTORE_NAME / folder_name
        snapshot_path.mkdir(parents=True, exist_ok=True)

        db_file = Path(settings.DATABASES['default']['NAME'])
        if db_file.exists():
            self.copy_sqlite_database(db_file, snapshot_path / db_file.name)

        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists():
            shutil.copytree(media_root, snapshot_path / 'media')

        self.cleanup_old_backups(name=self.PRE_RESTORE_NAME, keep=self.RETAIN_PRE_RESTORE)

        logger.info(f"Current data saved to {snapshot_path} before restoring.")
        return snapshot_path