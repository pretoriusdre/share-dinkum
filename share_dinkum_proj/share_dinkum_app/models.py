# Standard library imports
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# Django imports
from django.db import models, transaction
from django.core.files.temp import NamedTemporaryFile
from django.core.files.base import ContentFile
from django.contrib.auth.models import AbstractUser
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.urls import reverse
from django.db.models import Sum, Q

# Djmoney imports
from djmoney.models.fields import MoneyField, CurrencyField
from djmoney.settings import CURRENCY_CHOICES
from djmoney.contrib.exchange.models import convert_money
from djmoney.money import Money

# Local app imports
from share_dinkum_app import loading
from share_dinkum_app import excelinterface
from share_dinkum_app import yfinanceinterface
from share_dinkum_app.utils.currency import add_currencies
from share_dinkum_app.utils.filefield_operations import user_directory_path



from share_dinkum_app.decorators import safe_property


# Local but to be replaced in future
from share_dinkum_app.uuid_future  import uuid7 # Change this to "from uuid import uuid7", once this method is available in standard library

# global constant:
DEFAULT_CURRENCY = 'AUD'


class AppUser(AbstractUser):
    MODEL_DESCRIPTION = 'User accounts registered in the application.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    default_account = models.ForeignKey('Account', on_delete=models.SET_NULL, null=True, blank=True)

    def save(self, *args, **kwargs):
        # Ensure first_name and last_name are not None
        self.first_name = self.first_name or ''
        self.last_name = self.last_name or ''

        super().save(*args, **kwargs)



class FiscalYearType(models.Model):
    MODEL_DESCRIPTION = 'A system table used to define configuration for the financial year, such as its start day and month.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    description = models.CharField(max_length=40, default='Australian Tax Year', unique=True)
    start_month = models.IntegerField(default=7) # July
    start_day = models.IntegerField(default=1) # 1st (Australia)
    #legacy_id = models.CharField(max_length=36, null=True, blank=True, editable=False)


    def classify_date(self, input_date):
        """
        Get or create a FiscalYear based on an arbitrary date.

        :param input_date: A date within the fiscal year.
        :return: A tuple of (FiscalYear instance, created (True if created, False if retrieved)).
        """

        # Compute the fiscal start date for the given arbitrary date
        fiscal_start_date = date(input_date.year, self.start_month, self.start_day)

        # Determine the start year of the fiscal year
        if input_date >= fiscal_start_date:
            start_year = input_date.year
        else:
            start_year = input_date.year - 1

        # Use get_or_create to retrieve or create the FiscalYear instance
        fiscal_year, created = FiscalYear.objects.get_or_create(
            fiscal_year_type=self,
            start_year=start_year
        )

        return fiscal_year
    

    def __str__(self):
        return self.description
    
    def save(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)



class FiscalYear(models.Model):
    MODEL_DESCRIPTION = 'A particular fiscal year'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['fiscal_year_type', 'start_year'], name='fiscal_year_keys')
        ]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    fiscal_year_type = models.ForeignKey(FiscalYearType, on_delete=models.CASCADE)
    start_year = models.IntegerField(editable=False)

    name = models.CharField(max_length=9, null=True, blank=True, editable=False
                            )
    def __str__(self):
        return self.name or ''
       
    
    
    @safe_property
    def start_date(self):
        return date(self.start_year, self.fiscal_year_type.start_month, self.fiscal_year_type.start_day)

    
    @safe_property
    def end_date(self):
        end_year = self.start_year + 1 if self.fiscal_year_type.start_month != 1 else self.start_year
        return date(end_year, self.fiscal_year_type.start_month, self.fiscal_year_type.start_day) - timedelta(days=1)
    
    def get_name(self):
        if self.fiscal_year_type.start_month == 1:
            return f'{self.start_year}'
        else:
            return f'FY{self.start_year}/{f'{str(self.start_year + 1)[2:]}'}'
        
    def save(self, *args, **kwargs):
        self.name = self.get_name()

        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)



class Account(models.Model):
    MODEL_DESCRIPTION = 'Represents a particular portfolio.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    description = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    owner = models.ForeignKey(AppUser, on_delete=models.PROTECT)
    fiscal_year_type = models.ForeignKey(FiscalYearType, on_delete=models.PROTECT)
    #legacy_id = models.CharField(max_length=36, null=True, blank=True, editable=False)

    update_price_history = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.description} | {self.currency}'

    calculated_portfolio_value_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, default_currency=DEFAULT_CURRENCY)

    
    @safe_property
    def portfolio_value_converted(self):
        return Instrument.objects.filter(account=self, is_active=True).aggregate(models.Sum('calculated_value_held_converted'))['calculated_value_held_converted__sum'] or Money(0, self.currency)


    def update_all_price_history(self):
        """Update price history for all instruments with a position > 0 in this account."""
        instruments = Instrument.objects.filter(account=self, is_active=True)

        for instrument in instruments:
            if instrument.quantity_held > 0:  # Only update instruments with a position > 0
                instrument.update_price_history()

    def update_all_exchange_rate_history(self):

        convert_to = self.currency
        # Get distinct currencies based on the currency of  unit_price = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY) in the Buy model
        convert_from_currencies = (
            Buy.objects.filter(account=self)
            .values_list("unit_price_currency", flat=True)
            .distinct()
        )
        # Update exchange rate history for each currency
        for convert_from in convert_from_currencies:
            if convert_from != self.currency:
                ExchangeRate.update_exchange_rate_history(account=self, convert_from=convert_from, convert_to=self.currency)


    def save(self, *args, **kwargs):
        portfolio_value_converted = self.portfolio_value_converted
        self.calculated_portfolio_value_converted = portfolio_value_converted
        self.calculated_portfolio_value_converted_currency = self.currency

        if self.update_price_history:
            self.update_all_price_history()
            self.update_all_exchange_rate_history()

            
            self.update_price_history = False
            
        user = kwargs.pop('user', None)

        if self.owner.default_account is None:
            self.owner.default_account = self
            self.owner.save()
        super().save(*args, **kwargs)



class BaseModel(models.Model):
    MODEL_DESCRIPTION = 'Base Model'
    class Meta:
        abstract = True
        ordering = ['id'] 

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    legacy_id = models.CharField(max_length=36, null=True, blank=True, editable=False)

    description = models.CharField(max_length=255, null=True, blank=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    is_active = models.BooleanField(default=True, editable=False)
    notes = models.TextField(null=True, blank=True)


    
    @safe_property
    def associated_logs(self):
        content_type = ContentType.objects.get_for_model(self)
        log_entries = LogEntry.objects.filter(account=self.account, content_type=content_type, object_id=self.id)
        # Return a list of string representations of the log entries
        return '\n'.join([str(log_entry) for log_entry in log_entries]) 
    
    def log_event(self, event):
        content_type = ContentType.objects.get_for_model(self)
        LogEntry.objects.create(
            account=self.account,
            event=event,
            content_type=content_type,
            object_id=self.id,
            content_object=self
        )

    def get_absolute_url(self):
        # Redirect stuff to admin
        app_label = self._meta.app_label
        model_name = self._meta.model_name
        return reverse(f'admin:{app_label}_{model_name}_change', args=[str(self.id)])
    

    def save(self, *args, **kwargs):
        # Extract the user if provided
        user = kwargs.pop('user', None)
        # Call the parent class's save method to persist the instance first
        super().save(*args, **kwargs)
        
        # Update calculated fields after the object is saved
        for attr_name in dir(self):
            try:
                attr = getattr(self, attr_name)
                expected_calc_field_name = f"calculated_{attr_name}"
                if hasattr(self, expected_calc_field_name):
                    # Set the calculated field value
                    setattr(self, expected_calc_field_name, attr)

                # Where the currency differs from the base currency, set an exchange rate if not provided
                if attr_name.endswith('_currency'):
                    if attr and attr != self.account.currency and hasattr(self, 'exchange_rate'):
                        if not getattr(self, 'exchange_rate'):
                            exchange_rate_obj = ExchangeRate.get_or_create(account=self.account, convert_from=attr, convert_to=self.account.currency, exchange_date=self.date)
                            setattr(self, 'exchange_rate', exchange_rate_obj)

            except AttributeError:
                # Ignore attributes that cannot be accessed
                continue

        # Save again only if calculated fields were updated
        if any(f"calculated_{attr_name}" for attr_name in dir(self)):
            super().save(update_fields=[
                field.name for field in self._meta.get_fields() 
                if field.name.startswith("calculated_")
            ])
            
    def __str__(self):
        return f'{self.description}'



class LogEntry(BaseModel):
    MODEL_DESCRIPTION = 'Log entries. Key events are recorded here.'
    event = models.CharField(max_length=255)
    notes = None # Don't want notes on logs
    # Generic Foreign Key fields
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name='logs')
    object_id = models.UUIDField()
    content_object = GenericForeignKey('content_type', 'object_id')

    def __str__(self):
        return f'{self.created_at.isoformat(timespec="seconds")} - ***{(str(self.pk))[-4:]} - {self.event}'

class ExchangeRate(BaseModel):
    MODEL_DESCRIPTION = 'Exchange rates between pairs of currencies at particular dates.'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'convert_from', 'convert_to', 'date'], name='exchange_rate_keys')
        ]
        indexes = [
            models.Index(fields=['account', 'convert_from', 'convert_to', 'date'], name='exchange_rate_idx')
        ]
    
    convert_to = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    convert_from = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    date = models.DateField()
    exchange_rate_multiplier = models.DecimalField(max_digits=16, decimal_places=6, default=1.0)
    is_continuous_history = models.BooleanField(default=False, editable=False)

    @classmethod
    def get_or_create(cls, account, convert_from, convert_to, exchange_date):
        try:
            return cls.objects.get(
                account=account,
                convert_from=convert_from,
                convert_to=convert_to,
                date=exchange_date
            )
        except cls.DoesNotExist:
            # Ensure the record is created if it does not exist
            obj, created = cls.objects.get_or_create(
                account=account,
                convert_from=convert_from,
                convert_to=convert_to,
                date=exchange_date,
                defaults={'exchange_rate_multiplier' : 1.0}
            )
            if created:
                # Optionally update exchange rate history after creation
                obj.exchange_rate_multiplier = yfinanceinterface.get_exchange_rate(
                    convert_from=convert_from,
                    convert_to=convert_to,
                    exchange_date=exchange_date
                    )
                obj.save()
            return obj
        

    @classmethod
    def update_exchange_rate_history(cls, account, convert_from, convert_to):

        if convert_from == convert_to:
            return
        
        start_date = None

        # Fetch the latest price history entry for the related instrument
        latest_continuous_exchange_rate = ExchangeRate.objects.filter(account=account, convert_from=convert_from, convert_to=convert_to, is_continuous_history=True).order_by('-date').first()
        if latest_continuous_exchange_rate:
            start_date = latest_continuous_exchange_rate.date

        if not start_date:
            earliest_buy = Buy.objects.filter(account=account).order_by('date').first()
            start_date = earliest_buy.date

        if not start_date:
            return
        
        try:
            price_history = yfinanceinterface.get_exchange_rate_history(convert_from=convert_from, convert_to=convert_to, start_date=start_date)

            price_history['account'] = account
            price_history['id'] = price_history['date'].apply(lambda x : uuid7())
            

            # Bulk insert/update price history
            price_history_entries = []
            for _, row in price_history.iterrows():
                price_history_entries.append(
                    ExchangeRate(
                        **row.to_dict()
                    )
                )

            # Use bulk_create with `ignore_conflicts=True` to avoid duplicate errors
            with transaction.atomic():
                ExchangeRate.objects.bulk_create(price_history_entries, ignore_conflicts=True)



        except Exception as e:
            print(f'Error getting exchange rate history for {convert_from} to {convert_to}, {e}')


    def apply(self, money):
        
        if str(money.currency) != str(self.convert_from): # TODO REMOVE
            print(f'{money=}')
            print(f'{self.convert_from=}')

        assert str(money.currency) == str(self.convert_from), f'Invalid exchange rate applied. The convert_from currency {self.convert_from} does not match the currency {money.currency}'
        new_amount = money.amount * self.exchange_rate_multiplier
        return money.__class__(new_amount, self.convert_to)

    def __str__(self):
        return f'1 {self.convert_from} = {self.exchange_rate_multiplier} {self.convert_to} on {self.date.isoformat()}'



class Market(BaseModel):

    MODEL_DESCRIPTION = 'Share markets, such as ASX, NASDAQ, LSE, etc'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'code'], name='market_keys')
        ]

    code = models.CharField(max_length=16)

    suffix = models.CharField(max_length=16, null=True, blank=True)



class Instrument(BaseModel):
    MODEL_DESCRIPTION = 'Share codes, eg BHP, VGS, VAS, etc'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['name', 'account'], name='instrument_keys')
        ]

    name = models.CharField(max_length=16)

    description = models.CharField(max_length=255, blank=True)

    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)

    market = models.ForeignKey(Market, on_delete=models.PROTECT)

    current_unit_price = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True)


    calculated_quantity_held = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True, editable=False)
    
    @safe_property
    def quantity_held(self):
        total_bought = Buy.objects.filter(is_active=True, instrument=self).aggregate(models.Sum('quantity'))['quantity__sum'] or 0
        total_sold = Sell.objects.filter(is_active=True, instrument=self).aggregate(models.Sum('quantity'))['quantity__sum'] or 0
        return total_bought - total_sold


    calculated_value_held =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def value_held(self):
        if self.current_unit_price:
            return Money(self.current_unit_price * self.quantity_held, self.currency)
        if self.quantity_held > 0:
            return None
        return Money(0, self.currency) # zero quantity held

    calculated_value_held_converted =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def value_held_converted(self):
        if self.currency == self.account.currency:
            return self.value_held
        else:
            exchange_rate = ExchangeRate.objects.filter(account=self.account, convert_from=self.currency, convert_to=self.account.currency).order_by('-date').first()
            return exchange_rate.apply(self.value_held)


    
    @safe_property
    def yfinance_ticker_code(self):
        
        suffix = self.market.suffix
        
        if suffix:
            suffix = suffix.replace('.', '')
            return f'{self.name}.{suffix}'
        else:
            return self.name


    def __str__(self):
        if self.is_active:
            return f'{self.name} - {self.description}'
        else:
            return f'{self.name} - {self.description} (INACTIVE)'


    def update_price_history(self):
        # Fetch the latest price history entry for the related instrument
        latest_price_history = InstrumentPriceHistory.objects.filter(instrument=self).order_by('-date').first()

        # Determine the start date
        if latest_price_history:
            start_date = latest_price_history.date + timedelta(days=1)
        else:
            earliest_buy = Buy.objects.filter(instrument=self).order_by('date').first()
            if earliest_buy:
                start_date = earliest_buy.date
            else:
                start_date = date(2020, 1, 1)

        try:
            price_history = yfinanceinterface.get_instrument_price_history(instrument=self, start_date=start_date)

            price_history['account'] = self.account
            price_history['id'] = price_history['date'].apply(lambda x : uuid7())
            

            self.current_unit_price = Decimal(list(price_history['close'])[-1])
            self.save()
            
            # Bulk insert/update price history
            price_history_entries = []
            for _, row in price_history.iterrows():
                price_history_entries.append(
                    InstrumentPriceHistory(
                        **row.to_dict()
                    )
                )

            # Use bulk_create with `ignore_conflicts=True` to avoid duplicate errors
            with transaction.atomic():
                InstrumentPriceHistory.objects.bulk_create(price_history_entries, ignore_conflicts=True)

        except Exception as e:
            print(f'Error getting price history for {self}, {e}')


class InstrumentPriceHistory(models.Model):
    MODEL_DESCRIPTION = 'Price history for instruments.'
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'instrument', 'date'], name='instrument_price_history_keys')
        ]
        indexes = [
            models.Index(fields=['account', 'instrument', 'date'], name='instrument_date_idx')
        ]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=False)
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE,  editable=False)
    date = models.DateField(editable=False)
    
    open = models.DecimalField(max_digits=16, decimal_places=6, editable=False)
    high = models.DecimalField(max_digits=16, decimal_places=6, editable=False)
    low = models.DecimalField(max_digits=16, decimal_places=6, editable=False)
    close = models.DecimalField(max_digits=16, decimal_places=6, editable=False)
    volume = models.BigIntegerField(editable=False)
    stock_splits = models.DecimalField(max_digits=16, decimal_places=6, editable=False)


    class Meta:
        ordering = ['date'] 

    def get_absolute_url(self):
        # Redirect stuff to admin
        app_label = self._meta.app_label
        model_name = self._meta.model_name
        return reverse(f'admin:{app_label}_{model_name}_change', args=[str(self.id)])




class Trade(BaseModel):
    MODEL_DESCRIPTION = 'A base class for trades, such as buys and sells.'
    class Meta:
        abstract = True

    description = models.CharField(max_length=255, null=True, blank=True, editable=False) # Setting this automatically

    instrument = models.ForeignKey(Instrument, related_name='%(class)s', on_delete=models.PROTECT)
    date = models.DateField()
    quantity = models.DecimalField(max_digits=16, decimal_places=4)
    unit_price = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY)
    total_brokerage = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY)
    exchange_rate = models.ForeignKey(ExchangeRate, related_name='%(class)s', on_delete=models.PROTECT, blank=True, null=True)
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path)

    # Calculated fields
    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    
    @safe_property
    def fiscal_year(self):
        return self.account.fiscal_year_type.classify_date(input_date=self.date)

    
    calculated_total_brokerage_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def total_brokerage_converted(self):
        total_brokerage_converted =  self.total_brokerage
        if self.exchange_rate:
            total_brokerage_converted = self.exchange_rate.apply(total_brokerage_converted)
        return total_brokerage_converted
    

    calculated_unit_brokerage_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def unit_brokerage_converted(self):
        unit_brokerage_converted =  self.total_brokerage / self.quantity
        if self.exchange_rate:
            unit_brokerage_converted = self.exchange_rate.apply(unit_brokerage_converted)
        return unit_brokerage_converted
    

    calculated_unit_price_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def unit_price_converted(self):
        unit_price_converted =  self.unit_price
        if self.exchange_rate:
            unit_price_converted = self.exchange_rate.apply(unit_price_converted)
        return unit_price_converted


    def __str__(self):
        return f'{self.description}'


    def save(self, *args, **kwargs):
        if self.is_active:
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name} | {self.quantity} unit @ {self.unit_price_converted} / unit'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)


class Buy(Trade):
    MODEL_DESCRIPTION = 'Purchases of share parcels.'
    _creation_handled = models.BooleanField(default=False, editable=False)


    calculated_related_parcels = models.TextField(null=True, blank=True, editable=False)
    
    @safe_property
    def related_parcels(self):
        related_parcels = Parcel.objects.filter(buy=self)
        parcel_list ='\n'.join([str(parcel) for parcel in related_parcels])
        return parcel_list


    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self._creation_handled:
            parcel = Parcel(
                account=self.account,
                buy=self,
                parent_parcel=None,
                parcel_quantity=self.quantity,
                activation_date=self.date
            )
            parcel.save()
            self._creation_handled = True
            super().save(*args, **kwargs)
            message = f'This parcel was created from trade {self}'
            parcel.log_event(message)

        # Update position
        self.instrument.save()

    # TODO find a working solution. Django delete uses raw sql that bypasses this.
    def delete(self, *args, **kwargs):
        # Store references to related objects before deletion
        instrument = self.instrument
        # Delete the object
        super().delete(*args, **kwargs)
        # update the balances on the instrument
        instrument.save()

class Sell(Trade):
    MODEL_DESCRIPTION = 'Sales of shares.'
    _creation_handled = models.BooleanField(default=False, editable=False)

    STRATEGY_CHOICES = [
        ('FIFO', 'First-in, First-out'),
        ('LIFO', 'Last-in, First out'),
        ('MIN_CGT', 'Minimise net capital gain'),
        ('MANUAL', 'Manually create allocations')
    ]
    
    strategy = models.CharField(
        max_length=7,
        choices=STRATEGY_CHOICES,
        default='MIN_CGT',
    )

    calculated_proceeds = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def proceeds(self):
        proceeds = (self.quantity * self.unit_price_converted) - self.total_brokerage_converted
        return proceeds
    
    calculated_unit_proceeds = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def unit_proceeds(self):
        return self.proceeds / self.quantity

    calculated_unallocated_quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def unallocated_quantity(self):
        allocated_quantity = self.sale_allocation.filter(is_active=True).aggregate(total_allocated=Sum('quantity'))['total_allocated'] or 0
        return (self.quantity or 0 ) - allocated_quantity
    

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self._creation_handled:
            if self.strategy == 'MANUAL':
                self._creation_handled = True
                super().save(*args, **kwargs)

                return
            
            available_parcels = Parcel.objects.filter(account=self.account) \
                            .filter(deactivation_date__isnull=True) \
                            .filter(buy__instrument=self.instrument) \
                            .filter(buy__date__lte=self.date)
            
            
            if self.strategy == 'FIFO':
                available_parcels = available_parcels.order_by('buy__date')
            elif self.strategy == 'LIFO':
                available_parcels = available_parcels.order_by('-buy__date')
            elif self.strategy == 'MIN_CGT':
            
                unit_proceeds = self.unit_proceeds

                def get_unit_net_capital_gain(parcel):

                    capital_gain = unit_proceeds - parcel.unit_cost_base

                    if (self.date - parcel.buy.date).days > 365:
                        capital_gain *= 0.5
                    return capital_gain
                
                available_parcels = sorted(available_parcels, key=lambda parcel: get_unit_net_capital_gain(parcel))

            available_parcels = [parcel for parcel in available_parcels if parcel.unsold_quantity > 0]

            quantity_to_allocate = self.quantity
            
            for parcel in available_parcels:
                parcel_quantity = parcel.parcel_quantity
                qty_for_parcel = min(parcel_quantity, quantity_to_allocate)
                allocation = SellAllocation(
                    account=self.account,
                    parcel=parcel,
                    sell=self,
                    quantity=qty_for_parcel
                    )
                allocation.save()
                quantity_to_allocate -= qty_for_parcel
                if quantity_to_allocate <= 0:
                    break
        self._creation_handled = True
        super().save(*args, **kwargs)

        # Update position
        self.instrument.save()

class Parcel(BaseModel):
    MODEL_DESCRIPTION = 'Collections of shares with the same unit properties. Can be split into other parcels.'
    description = models.CharField(max_length=255, null=True, blank=True, editable=False) # Setting this automatically

    buy = models.ForeignKey(Buy, related_name='parcels', on_delete=models.CASCADE, editable=False)
    parent_parcel = models.ForeignKey(
        'self',
        related_name='children',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        editable=False
        )
    parcel_quantity = models.DecimalField(max_digits=16, decimal_places=4, editable=False)
    cumulative_split_multiplier = models.DecimalField(max_digits=16, decimal_places=4, editable=False, default=1.0)
    
    activation_date = models.DateField(null=True, editable=False)
    deactivation_date = models.DateField(null=True, editable=False)


    calculated_unsold_quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, editable=False)

    @safe_property
    def unsold_quantity(self):
        sold_quantity = self.sale_allocation.filter(is_active=True).aggregate(total_allocated=Sum('quantity'))['total_allocated'] or 0
        return self.parcel_quantity - sold_quantity


    calculated_is_sold = models.BooleanField(null=True, blank=True, editable=False)
    
    @safe_property
    def is_sold(self):
        return self.unsold_quantity == 0

    calculated_is_sold = models.BooleanField(null=True, blank=True, editable=False)
    
    @safe_property
    def adjusted_buy_price(self):
        adjusted_buy_price = self.buy.unit_price_converted / self.cumulative_split_multiplier
        return adjusted_buy_price

    calculated_adjusted_unit_brokerage = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def adjusted_unit_brokerage(self):
        adjusted_unit_brokerage = self.buy.unit_brokerage_converted / self.cumulative_split_multiplier
        return adjusted_unit_brokerage


    calculated_total_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_cost_base(self):
        # Already converted
        parcel_quantity = self.parcel_quantity
        total_cost_base = (self.adjusted_buy_price * parcel_quantity)
        total_cost_base += (self.adjusted_unit_brokerage * parcel_quantity)

        return total_cost_base
    

    calculated_unit_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def unit_cost_base(self):
        return self.total_cost_base / self.parcel_quantity

    def split_or_consolidate(self, multiplier, date):
        assert multiplier > 0
        assert self.is_active

        new_parcel_message = f'This parcel was created by splitting parcel {self.pk} by multiplier {multiplier}'

        with transaction.atomic():
            # Create target parcel
            parcel_target = Parcel.objects.get(pk=self.pk)
            parcel_target.pk = None # Make a new instance
            parcel_target.activation_date = date # Set new activation date
            parcel_target.parent_parcel = self
            parcel_target.parcel_quantity *= multiplier
            parcel_target.cumulative_split_multiplier *= multiplier
            parcel_target.save()
            parcel_target.log_event(new_parcel_message)

            # Update old parcel
            self.log_event(f'This parcel was split with multipler {multiplier}, then marked as INACTIVE. New parcel is {parcel_target.pk}.')
            
            # This sets is_active = False for the old parcel
            self.deactivation_date = date

            self.save() # not needed as add_note also saves
        
    def bifurcate(self, quantity, date):
        assert quantity > 0, "Quantity to bifurcate (split) must be greater than zero"
        assert quantity <= self.parcel_quantity, "Quantity to bifurcate (split) must be less than the available quantity"
        assert self.is_active

        if quantity == self.parcel_quantity:
            # No need to bifurcate.
            return self
        
        new_parcel_message = f'This parcel was created by splitting parcel {self.pk} into two separate parcels.'

        remainder_quantity = self.parcel_quantity - quantity

        with transaction.atomic():
            # Create target parcel
            parcel_target = Parcel.objects.get(pk=self.pk)
            parcel_target.pk = None
            parcel_target.activation_date = date # Set new activation date
            parcel_target.parent_parcel = self
            parcel_target.parcel_quantity = quantity
            parcel_target.save()
            parcel_target.log_event(new_parcel_message)
            # Create remainder parcel
            parcel_remainder = Parcel.objects.get(pk=self.pk)
            parcel_remainder.pk = None
            parcel_target.activation_date = date # Set new activation date
            parcel_remainder.parent_parcel = self
            parcel_remainder.parcel_quantity = remainder_quantity
            parcel_remainder.save()
            parcel_remainder.log_event(new_parcel_message)
            # Update old parcel
            self.log_event(f'This parcel was split into {parcel_target.pk} and {parcel_remainder.pk}, then marked as INACTIVE')
            
            # self.is_active = False
            self.deactivation_date = date

            self.save()

            related_adjustments = CostBaseAdjustmentAllocation.objects.filter(account=self.account) \
                .filter(parcel=self) \
                .filter(parcel__buy__instrument=self.buy.instrument) \
                .filter(is_active=True)
            
            target_fraction = quantity / (quantity + remainder_quantity)
            for adjustment in related_adjustments:
                adjustment.bifurcate(
                    target_parcel=parcel_target,
                    remainder_parcel=parcel_remainder,
                    target_fraction=target_fraction,
                    date=date
                    )


        return parcel_target

    def __str__(self):
        if self.is_active:
            packacke_desc = f'{self.description} @ {self.adjusted_buy_price} / unit | Total cost base = {self.total_cost_base} |'
            if self.is_sold:
                packacke_desc += ' SOLD'
            return packacke_desc 
        else:
            return f'{self.pk} | INACTIVE'

    def save(self, *args, **kwargs):

        self.is_active = self.deactivation_date is None
        self.description = f'{self.buy.date} | PARCEL |  {self.buy.instrument.name} | {self.parcel_quantity} unit'

        super().save(*args, **kwargs)



class SellAllocation(BaseModel):
    MODEL_DESCRIPTION = 'Allocations of sell events to specific parcels.'
    description = models.CharField(max_length=255, null=True, blank=True, editable=False) # Setting this automatically

    _creation_handled = models.BooleanField(default=False, editable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    parcel = models.ForeignKey(Parcel, related_name='sale_allocation', on_delete=models.PROTECT)
    sell = models.ForeignKey(Sell, related_name='sale_allocation', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=16, decimal_places=4)


    calculated_sale_date = models.DateField(null=True, blank=True, editable=False)
    
    @safe_property
    def sale_date(self):
        return self.sell.date
    

    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    
    @safe_property
    def fiscal_year(self):
        return self.account.fiscal_year_type.classify_date(input_date=self.sale_date)
    

    calculated_days_held = models.IntegerField(null=True, blank=True, editable=False)
    
    @safe_property
    def days_held(self):
        return (self.sell.date - self.parcel.buy.date).days
    

    calculated_total_capital_gain = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_capital_gain(self):
        return (self.sell.proceeds * self.quantity / self.sell.quantity) - self.parcel.total_cost_base




    def save(self, *args, **kwargs):

        if self.is_active:
            self.description = f'{self.sell.date} {self.sell.instrument.name} | {self.quantity}'
        else:
            self.description = 'INACTIVE'

        if not self._creation_handled:
            
            # bifurcate the parcel (split into two uneven parcels)
            self.parcel = self.parcel.bifurcate(quantity=self.quantity, date=self.sell.date)
            self._creation_handled = True
            
            super().save(*args, **kwargs)

        # In any case, save the object
        super().save(*args, **kwargs)

        #save the sell and parcel to update qty
        self.parcel.save()
        self.sell.save()

    # TODO find a working solution. Django delete uses raw sql that bypasses this.
    def delete(self, *args, **kwargs):
        # Store references to related objects before deletion
        parcel = self.parcel
        sell = self.sell

        # Delete the SellAllocation instance
        super().delete(*args, **kwargs)

        # Call save on the related objects after deletion
        parcel.save()
        sell.save()


class ShareSplit(BaseModel):
    MODEL_DESCRIPTION = 'Events which transform parcels into new parcels with different cost base and quantity.'
    instrument = models.ForeignKey(Instrument, related_name='share_split', on_delete=models.PROTECT)
    quantity_before = models.DecimalField(max_digits=16, decimal_places=4)
    quantity_after = models.DecimalField(max_digits=16, decimal_places=4)
    date = models.DateField()
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path)
    affected_parcels = models.ManyToManyField(Parcel, editable=False)
    _creation_handled = models.BooleanField(default=False, editable=False)


    calculated_split_multiplier = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def split_multiplier(self):
        multiplier = self.quantity_after / self.quantity_before
        return multiplier.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

    calculated_affected_parcels = models.TextField(null=True, blank=True, editable=False)
    
    @safe_property
    def affected_parcel_list(self):
        parcels = self.affected_parcels.select_related()
        parcel_list_str = ''
        for parcel in parcels:
            parcel_list_str += f'{parcel}\n'
        return parcel_list_str

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self._creation_handled:
            # bifurcate the parcel (split into two uneven parcels)
            # self.parcel = self.parcel.bifurcate(self.quantity)
            with transaction.atomic():
                split_multiplier = self.split_multiplier
                for parcel in Parcel.objects.filter(account=self.account) \
                        .filter(deactivation_date__isnull=True) \
                        .filter(buy__instrument=self.instrument) \
                        .filter(buy__date__lte=self.date):
                    if not parcel.is_sold:
                        parcel.split_or_consolidate(multiplier=split_multiplier, date=self.date)
                        self.affected_parcels.add(parcel)

                # In any case, save the object
                self._creation_handled = True
                super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.pk} | {self.date} | Split of {self.instrument.name} | Multiplier = {self.split_multiplier}'

class CostBaseAdjustment(BaseModel):
    MODEL_DESCRIPTION = 'Cost base adjustments applied to instruments, i.e. AMIT cost base adjustments.'
    cost_base_increase = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY)
    instrument = models.ForeignKey(Instrument, related_name='cost_base_adjustment', on_delete=models.PROTECT)
    financial_year_end_date = models.DateField()

    exchange_rate = models.ForeignKey(ExchangeRate, related_name='cost_base_adjustment', on_delete=models.PROTECT, blank=True, null=True)
    
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path)
    
    _creation_handled = models.BooleanField(default=False, editable=False)


    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    
    @safe_property
    def fiscal_year(self):
        return self.account.fiscal_year_type.classify_date(input_date=self.financial_year_end_date)
    

    @property
    def date(self):
        # Alias as it is used in the user_directory_path
        return self.financial_year_end_date
    


    calculated_cost_base_increase_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def cost_base_increase_converted(self):
        cost_base_increase_converted =  self.cost_base_increase
        if self.exchange_rate:
            cost_base_increase_converted = self.exchange_rate.apply(cost_base_increase_converted)
        return cost_base_increase_converted
    

    ALLOCATION_CHOICES = [
        ('QTY_HELD', 'Alllocate to parcels, weighting by (qty * days_held) in the F.Y.'),
        ('MANUAL', 'Manually create allocations')
    ]
    allocation_method = models.CharField(
        max_length=8,
        choices=ALLOCATION_CHOICES,
        default='QTY_HELD',
    )
    def get_description(self):
        return f'{self.pk} | {self.financial_year_end_date} | Adjustment of {self.instrument.name} | Cost base increase = {self.cost_base_increase}'
    
    def save(self, *args, **kwargs):
        self.description = self.get_description()

        super().save(*args, **kwargs)
        if not self._creation_handled:
            if self.allocation_method == 'QTY_HELD':
                end = self.financial_year_end_date
                cutoff_date = date(end.year - 1, end.month, end.day) + timedelta(days=1) # 30 June 2025 becomes 1 July 2024. Accounts for leap yr.
                with transaction.atomic():
                    affected_parcels = Parcel.objects.filter(account=self.account) \
                        .filter(buy__instrument=self.instrument) \
                        .filter(Q(deactivation_date__isnull=True) | Q(deactivation_date__gte=cutoff_date)) \
                        .filter(buy__date__lte=end)

                    affected_parcels = list(affected_parcels)

                    total_weighted_sum = 0
                    days_in_year = (end - cutoff_date).days + 1 # normally 365
                    for parcel in affected_parcels:
                        days_held = days_in_year
                        if parcel.deactivation_date:
                            days_held = min(days_held, (parcel.deactivation_date - cutoff_date).days + 1) # In range 1 to 365 inclusive
                        total_weighted_sum += (parcel.parcel_quantity * days_held)
                    for parcel in affected_parcels:
                        days_held = days_in_year
                        if parcel.deactivation_date:
                            days_held = min(days_held, (parcel.deactivation_date - cutoff_date).days + 1) # In range 1 to 365 inclusive
                        parcel_weight = (parcel.parcel_quantity * days_held)
                        adjustment_fraction =  parcel_weight / total_weighted_sum

                        adjustment_alllocation = CostBaseAdjustmentAllocation.objects.create(
                            account = self.account,
                            cost_base_increase=self.cost_base_increase_converted * adjustment_fraction,
                            parcel=parcel,
                            cost_base_adjustment=self
                        )
                        adjustment_alllocation.log_event(f'Added fraction {adjustment_fraction} of cost base adjustment {self}')
                    
            else:
                pass

            self._creation_handled = True
            super().save(*args, **kwargs)

    def __str__(self):
        return self.description

                


class CostBaseAdjustmentAllocation(BaseModel):
    MODEL_DESCRIPTION = 'Allocations of cost base adjustments to specific parcels.'

    cost_base_increase = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY)
    parcel = models.ForeignKey(Parcel, related_name='cost_base_adjustment_allocation', on_delete=models.PROTECT)
    cost_base_adjustment = models.ForeignKey(CostBaseAdjustment, related_name='cost_base_adjustment_allocation', on_delete=models.PROTECT) # TODO make cascade?


    def bifurcate(self, target_parcel, remainder_parcel, target_fraction, date):
        assert target_fraction >= 0, "Target fraction must be >= 0"
        assert target_fraction <= 1, "Target fraction must be <= 1"
        target_parcel_qty = target_parcel.parcel_quantity
        remainder_parcel_qty = remainder_parcel.parcel_quantity
        target_fraction = target_parcel_qty / (target_parcel_qty +  + remainder_parcel_qty)

        assert self.is_active

        new_parcel_message = f'This CostBaseAdjustmentAllocation was created by splitting {self.pk} into two separate allocations.'

        with transaction.atomic():
            # Create target allocation
            allocation_target = CostBaseAdjustmentAllocation.objects.get(pk=self.pk)
            allocation_target.pk = None
            allocation_target.activation_date = date
            allocation_target.parcel = target_parcel
            allocation_target.cost_base_increase *= target_fraction
            allocation_target.save()
            allocation_target.log_event(new_parcel_message)
            # Create remainder parcel
            allocation_remainder = CostBaseAdjustmentAllocation.objects.get(pk=self.pk)
            allocation_remainder.pk = None
            allocation_remainder.activation_date = date
            allocation_remainder.parcel = remainder_parcel
            allocation_remainder.cost_base_increase *= target_fraction
            allocation_remainder.save()
            allocation_remainder.log_event(new_parcel_message)
            # Update old parcel
            self.log_event(f'This allocation was split into {allocation_target.pk} and {allocation_remainder.pk}, then marked as INACTIVE')
            self.deactivation_date = date
            self.save()        
        return
    
    # TODO find a working solution. Django delete uses raw sql that bypasses this.
    def delete(self, *args, **kwargs):
        related_parcel = self.parcel
        super().delete(*args, **kwargs)
        related_parcel.save()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Update the affected parcel
        self.parcel.save()


    def __str__(self):
        return f'{self.pk} | {self.cost_base_adjustment.financial_year_end_date} | Adjustment of {self.cost_base_adjustment.instrument.name} | Cost base increase = {self.cost_base_increase} | applied to {self.parcel.id}'
    


# TODO make it so that it has a string method or updates descr

class Income(BaseModel):
    MODEL_DESCRIPTION = 'Base class for Income, eg Austrlalian Dividends and Distributions.'
    
    class Meta:
        abstract = True

    description = models.CharField(max_length=255, null=True, blank=True, editable=False) # Setting this automatically
    instrument = models.ForeignKey(Instrument, related_name='%(class)s', on_delete=models.PROTECT)

    date = models.DateField()
    quantity = models.DecimalField(max_digits=16, decimal_places=4)
    exchange_rate = models.ForeignKey(ExchangeRate, related_name='%(class)s', on_delete=models.PROTECT, blank=True, null=True)

    file = models.FileField(null=True, blank=True, upload_to=user_directory_path)

    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False)
    
    @safe_property
    def fiscal_year(self):
        return self.account.fiscal_year_type.classify_date(input_date=self.date)

    def save(self, *args, **kwargs):
        if self.is_active:
            # TODO include total income somehow
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name}' # | {self.quantity} unit @ {self.unit_price_converted} / unit'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)



class Dividend(Income):
    MODEL_DESCRIPTION = 'Dividends, including local dividends and foreign dividends.'

    DIVIDEND_TYPE_CHOICES = (
        ('LOCAL', 'Local dividend'),
        ('FOREIGN', 'Foreign dividend')
    )

    dividend_type = models.CharField(max_length=7, choices=DIVIDEND_TYPE_CHOICES, default='LOCAL')

    unfranked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)
    franked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)

    local_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)
    foreign_tax_credit = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)
    lic_capital_gain = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)

    corporate_tax_rate_percentage = models.DecimalField(
        max_digits=5,  # Total digits, including decimal places
        decimal_places=2,  # Number of digits after the decimal
        default=30.0,
        help_text="Enter a percentage value (e.g., 25.00 for 25%)"
    )

    # Don't care about this one
    
    @safe_property
    def company_rate(self):
        return self.corporate_tax_rate_percentage / 100

    calculated_total_unfranked_amount = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_unfranked_amount(self):
        return self.unfranked_amount_per_share * self.quantity

    calculated_total_franked_amount = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_franked_amount(self):
        return self.franked_amount_per_share * self.quantity
    

    calculated_total_franking_credits = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_franking_credits(self):
        return self.total_franked_amount * self.company_rate / (1 - self.company_rate)
    
    calculated_total_dividend = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_dividend(self):
        # handle zero amounts in wrong currency
        return add_currencies(self.total_unfranked_amount, self.total_franked_amount)


    calculated_total_dividend_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_dividend_converted(self):

        total_dividend_converted =  self.total_dividend
        if self.exchange_rate:
            total_dividend_converted = self.exchange_rate.apply(total_dividend_converted)
        return total_dividend_converted



class Distribution(Income):
    MODEL_DESCRIPTION = 'Distributions, such as the income received from ETFs'
    distribution_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)
    total_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=0)
    
    calculated_total_distribution = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_distribution(self):
        return self.distribution_amount_per_share * self.quantity
    
    calculated_total_distribution_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_distribution_converted(self):
        total_distribution_converted = self.total_distribution
        if self.exchange_rate:
            total_distribution_converted = self.exchange_rate.apply(total_distribution_converted)
        return total_distribution_converted



class DataExport(BaseModel):
    MODEL_DESCRIPTION = 'Data export events, referencing the associated output file.'
    file = models.FileField(null=True, blank=True, editable=False, upload_to=user_directory_path)

    account = models.ForeignKey(Account, on_delete=models.PROTECT)

    def save(self, *args, **kwargs):

        if not self.file:
            with NamedTemporaryFile(suffix='.xlsx') as temp_file:
                temp_filename = temp_file.name
                print(f'temp filename is {temp_filename}')

                gen = excelinterface.ExcelGen(title='Data Export')

                for model in apps.get_app_config('share_dinkum_app').get_models():

                    if 'account' in [field.name for field in model._meta.get_fields()]:
                        queryset = loading.model_to_queryset(model=model, account=self.account)
                    else:
                        queryset = loading.model_to_queryset(model=model)
                    
                    df = loading.queryset_to_df(queryset)
                    if hasattr(model, 'MODEL_DESCRIPTION'):
                        model_description = model.MODEL_DESCRIPTION
                    else:
                        model_description = 'No description available'
                    
                    if not df.empty:
                        gen.add_table(df, table_name=model.__name__, description=model_description)


                gen.save(temp_filename)

                new_name = f'Export_{self.account.description}.xlsx'
                self.file = ContentFile(
                    temp_file.file.read(), name=new_name
                )
                super().save(*args, **kwargs)
                print('done')

    def __str__(self):
        return f'{self.created_at.date().isoformat()} | Data Export - {self.account.description}'
    