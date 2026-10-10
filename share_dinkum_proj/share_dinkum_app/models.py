# Standard library imports
from datetime import date, timedelta, datetime, UTC
from decimal import Decimal, ROUND_HALF_UP
import bisect
import copy
from typing import Any, cast

# Django imports
from django.db import models, transaction
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.urls import reverse
from django.db.models import Sum, F, Q, QuerySet
from django.db.models.functions import Coalesce

# Djmoney imports
from djmoney.models.fields import MoneyField, CurrencyField
from djmoney.settings import CURRENCY_CHOICES
from djmoney.money import Money

# Local app imports
from share_dinkum_app import yfinanceinterface
from share_dinkum_app.utils import convert_to_decimal_field
from share_dinkum_app.utils.currency import add_currencies
from share_dinkum_app.utils.filefield_operations import user_directory_path
from share_dinkum_app.decorators import safe_property
from share_dinkum_app.choices import (
    AllocationMethod,
    AttributionComponent as AttributionComponentType,
    CGTAssetCategory,
    CGTBasis,
    DividendType,
    LegalForm,
    LegalFormSource,
    ResidencyStatus,
    SellStrategy,
    TaxpayerType,
    ValuationPurpose,
    ValuationSource,
)
from share_dinkum_app.constants import DEFAULT_CURRENCY


# Local but to be replaced in future
from share_dinkum_app.uuid_future  import uuid7 # Change this to "from uuid import uuid7", once this method is available in standard library

# Logging setup
import logging
logger = logging.getLogger(__name__)

# Annotations use this, not `date`: many models below have a `date` field, which would shadow
# the class in a method signature evaluated inside the class body.
Date = date


def validate_positive(value: Any) -> None:
    """More than zero. Parcels are multiplied and divided by these quantities."""
    if value is not None and value <= 0:
        raise ValidationError(f'This must be more than zero, not {value}.')


def validate_company_tax_rate(value: Any) -> None:
    """From 0 up to but not including 100. Franking credits divide by 1 less the rate."""
    if value is not None and not 0 <= value < 100:
        raise ValidationError(
            f'A company tax rate must be at least 0 and less than 100, not {value}.')




class AppUser(AbstractUser):
    MODEL_DESCRIPTION = 'User accounts registered in the application.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of the user.')
    default_account = models.ForeignKey('Account', on_delete=models.SET_NULL, null=True, blank=True,
        help_text='The portfolio shown first after signing in.')

    @property
    def visible_account(self) -> 'Account | None':
        """The user's default portfolio, else the first one they created.

        Shared by the dashboard and auto-login so both resolve the same portfolio.
        """
        return self.default_account or Account.objects.filter(owner=self).order_by('created_at').first()

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get('update_fields', None)

        # Only modify first_name/last_name if not using update_fields
        if not update_fields:
            self.first_name = self.first_name or ''
            self.last_name = self.last_name or ''

        super().save(*args, **kwargs)

class FiscalYearType(models.Model):
    MODEL_DESCRIPTION = 'A system table used to define configuration for the financial year.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of this fiscal year pattern.')
    description = models.CharField(max_length=40, default='Australian Tax Year', unique=True,
        help_text='Name of the fiscal year pattern, such as Australian Tax Year.')
    start_month = models.IntegerField(default=7,
        help_text='Month the fiscal year starts, 1 to 12. July (7) for the Australian tax '
                  'year.') # July
    start_day = models.IntegerField(default=1,
        help_text='Day of the month the fiscal year starts. 1 for the Australian tax year.') # 1st (Australia)

    def classify_date(self, input_date: Date) -> tuple['FiscalYear', bool]:
        """Get or create the FiscalYear containing `input_date`. Returns `(fiscal_year, created)`."""

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

        return (fiscal_year, created)
    

    def __str__(self) -> str:
        return self.description
    
    def save(self, *args: Any, **kwargs: Any) -> None:
        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)


class FiscalYear(models.Model):
    MODEL_DESCRIPTION = 'A particular fiscal year'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['fiscal_year_type', 'start_year'], name='fiscal_year_keys')
        ]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of this fiscal year.')

    fiscal_year_type = models.ForeignKey(FiscalYearType, on_delete=models.CASCADE,
        help_text='The fiscal year pattern this year belongs to.')
    start_year = models.IntegerField(editable=False,
        help_text='Calendar year the fiscal year starts in. FY2023/24 starts in 2023.')

    name = models.CharField(max_length=9, null=True, blank=True, editable=False,
        help_text='Display name, such as FY2023/24. Set by the app; do not edit.')

    def __str__(self) -> str:
        return self.name or ''
       
    @safe_property
    def start_date(self) -> date:
        return date(self.start_year, self.fiscal_year_type.start_month, self.fiscal_year_type.start_day)

    @safe_property
    def end_date(self) -> date:
        next_start_year = self.start_year + 1
        next_start = date(next_start_year,
                        self.fiscal_year_type.start_month,
                        self.fiscal_year_type.start_day)
        return next_start - timedelta(days=1)


    def get_name(self) -> str:
        if self.fiscal_year_type.start_month == 1:
            return f'{self.start_year}'
        else:
            return f'FY{self.start_year}/{str(self.start_year + 1)[2:]}'
        
    def save(self, *args: Any, **kwargs: Any) -> None:
        self.name = self.get_name()
        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)


class Account(models.Model):
    MODEL_DESCRIPTION = 'Represents a particular portfolio.'

    class Meta:
        constraints = [
            # Portfolios are looked up by description when loading a file, so two of one name would
            # make it ambiguous which one a file was meant for.
            models.UniqueConstraint(fields=['owner', 'description'], name='account_keys')
        ]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of the portfolio.')
    description = models.CharField(max_length=40,
        help_text='Name of the portfolio, as shown in the app. Unique for each owner.')
    created_at = models.DateTimeField(auto_now_add=True,
        help_text='When the portfolio was created.')
    updated_at = models.DateTimeField(auto_now=True,
        help_text='When the portfolio was last changed.')
    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES,
        help_text='Base currency, such as AUD. Foreign holdings and income are converted to '
                  'it.')
    owner = models.ForeignKey(AppUser, on_delete=models.PROTECT,
        help_text='The user who owns the portfolio.')
    fiscal_year_type = models.ForeignKey(FiscalYearType, on_delete=models.PROTECT,
        help_text='The fiscal year pattern reports use. The Australian Tax Year runs July '
                  'to June.')
    update_price_history = models.BooleanField(default=False,
        help_text='Whether the app downloads price and exchange rate history for this '
                  'portfolio.')


    #: Sets the CGT discount: half for an individual or trust, a third for a complying super
    #: fund, none for a company. Undeclared by default rather than guessed.
    taxpayer_type = models.CharField(
        max_length=11, choices=TaxpayerType.choices, default=TaxpayerType.UNDECLARED,
        help_text='Who owns this portfolio for tax purposes.')

    #: When set, silences the dashboard's tax settings warning.
    tax_settings_reviewed_at = models.DateTimeField(null=True, blank=True, editable=False,
        help_text='When the tax settings were confirmed. Once set, the dashboard stops '
                  'warning that they are undeclared.')

    #: Whether disposals from 1 July 2027 are worked out under the 2027 regime. Earlier
    #: disposals are unaffected.
    model_2027_regime = models.BooleanField(
        default=False,
        help_text='Model the 2027 capital gains changes for disposals from 1 July 2027. '
                  'Nothing before that date changes.')

    def __str__(self) -> str:
        return f'{self.description} | {self.currency}'

    calculated_portfolio_value_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, default_currency=DEFAULT_CURRENCY,
        help_text='Value of everything currently held, in the portfolio currency. Set by '
                  'the app; do not edit.')

    @safe_property
    def portfolio_value_converted(self) -> Money:
        return Instrument.objects.filter(account=self, is_active=True).aggregate(models.Sum('calculated_value_held_converted'))['calculated_value_held_converted__sum'] or Money(0, self.currency)

    #: Days to keep fetching a sold instrument that has no price on or after its last sale.
    #: A delisted security never gets one, so without a limit it is fetched forever.
    POST_SALE_PRICE_GRACE_DAYS = 7

    def update_all_price_history(self) -> None:
        """Update price history for this account's instruments.

        Open positions are always refreshed. A fully sold instrument is refreshed until it has
        a price on or after its last sale, or until `POST_SALE_PRICE_GRACE_DAYS` after the
        later of the sale date and the day the sale was recorded.
        """
        instruments = Instrument.objects.filter(account=self, is_active=True)
        today = date.today()

        for instrument in instruments:
            if instrument.quantity_held > 0:
                instrument.update_price_history()
                continue

            last_sell = (
                Sell.objects.filter(account=self, instrument=instrument)
                .order_by('-date')
                .first()
            )

            if last_sell is None:
                continue

            has_history_after_sell = InstrumentPriceHistory.objects.filter(
                account=self,
                instrument=instrument,
                date__gte=last_sell.date,
            ).exists()

            if has_history_after_sell:
                continue

            # Counted from whichever is later: the sale, or the day the sale was recorded.
            # The date alone would be wrong for a disposal entered months after the fact --
            # its window would have closed before the application ever heard of it, and the
            # price that *is* available would never be fetched. What the grace period is
            # really measuring is how long we have had the chance to look.
            recorded_on = last_sell.created_at.date() if last_sell.created_at else last_sell.date
            give_up_after = max(last_sell.date, recorded_on) + timedelta(
                days=self.POST_SALE_PRICE_GRACE_DAYS)
            if today > give_up_after:
                logger.debug(
                    'Not looking for a post-sale price for %s: sold %s, recorded %s, and '
                    'none has appeared. It is most likely no longer quoted.',
                    instrument, last_sell.date, recorded_on)
                continue

            instrument.update_price_history(end_date=today)


    def update_all_exchange_rate_history(self) -> None:

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

                # The staleness check in CurrentExchangeRate.get_or_create is based on updated_at,
                # which auto_now sets whenever the row is written, not on the age of the rate itself.
                # An import, restore or backfill therefore writes an old rate with a current timestamp
                # and it is then treated as fresh for the next hour. Force the fetch so the current
                # rate is genuinely current before any instrument is valued against it.
                CurrentExchangeRate.get_or_create(
                    account=self,
                    convert_from=convert_from,
                    convert_to=convert_to,
                    force_refresh=True,
                )

    def refresh_market_data(self) -> None:
        """Fetch exchange rates, then prices, and store the portfolio value they give.

        Rates come first. Saving an instrument stores its value converted at whatever the
        current rate is at that moment, and nothing re-converts it afterwards, so refreshing
        the rate second leaves every holding valued at the previous rate.
        """
        # Ideally run this as a background task (Celery, Django-Q, etc.)
        self.update_all_exchange_rate_history()
        self.update_all_price_history()
        self.update_price_history = False
        self.save(update_fields=['update_price_history'])  # save() adds the portfolio value

    CALCULATED_FIELDS = frozenset({
        'calculated_portfolio_value_converted',
        'calculated_portfolio_value_converted_currency',
    })

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.calculated_portfolio_value_converted = self.portfolio_value_converted
        self.calculated_portfolio_value_converted_currency = self.currency

        # Every save recomputes the portfolio value, so a partial save has to write it too.
        # Without this, a caller narrowing update_fields to its own field (as the price refresh
        # signal does) silently discards the freshly calculated total and leaves the stored
        # figure behind whatever the instruments now say.
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | self.CALCULATED_FIELDS

        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)


class BaseModel(models.Model):
    MODEL_DESCRIPTION = 'Base Model'
    class Meta:
        abstract = True
        ordering = ['id'] 

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier. Exports carry it so a file can be restored; leave it '
                  'out of an import template.')
    legacy_id = models.CharField(max_length=36, null=True, blank=True, editable=False,
        help_text='Your own reference for this row, such as B001. Rows in other tables '
                  'refer to it, and loading the file again matches on it, so keep it unique '
                  'within the table.')
    description = models.CharField(max_length=255, null=True, blank=True,
        help_text='Short description of the record.')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=True,
        help_text='The portfolio this record belongs to.')
    created_at = models.DateTimeField(auto_now_add=True, editable=False,
        help_text='When the record was created.')
    updated_at = models.DateTimeField(auto_now=True, editable=False,
        help_text='When the record was last changed.')
    is_active = models.BooleanField(default=True, editable=False,
        help_text='False once the record has been replaced or deactivated, such as a parcel '
                  'split by a sale. Inactive records are kept for the history.')
    notes = models.TextField(null=True, blank=True,
        help_text='Free text notes about the record.')

    @safe_property
    def associated_logs(self) -> str:
        content_type = ContentType.objects.get_for_model(self)
        log_entries = LogEntry.objects.filter(account=self.account, content_type=content_type, object_id=self.id)
        # Return a list of string representations of the log entries
        return '\n'.join([str(log_entry) for log_entry in log_entries]) 
    
    def log_event(self, event: str) -> None:
        content_type = ContentType.objects.get_for_model(self)
        LogEntry.objects.create(
            account=self.account,
            event=event,
            content_type=content_type,
            object_id=self.id,
            content_object=self
        )

    def get_absolute_url(self) -> str:
        # Redirect stuff to admin
        app_label = self._meta.app_label
        model_name = self._meta.model_name
        return reverse(f'admin:{app_label}_{model_name}_change', args=[str(self.id)])
    
    #: Fields other records were worked out from when this one was entered: a buy's quantity
    #: for its parcel, a split's ratio for the parcels it split. Nothing works them out
    #: again, so once the record has been handled they cannot be changed.
    STRUCTURAL_FIELDS: tuple[str, ...] = ()

    #: Fields that must be more than zero (`validate_positive`). A form checks them; an import
    #: does not, so a new record is checked again on save. Zero otherwise failed as a division
    #: by zero deep inside a signal, and a negative made a negative holding.
    POSITIVE_FIELDS: tuple[str, ...] = ()

    def _not_positive_message(self) -> str | None:
        """Why this new record cannot be saved because of a POSITIVE_FIELDS value, or None."""
        for name in self.POSITIVE_FIELDS:
            value = getattr(self, name)
            if value is not None and value <= 0:
                field: Any = self._meta.get_field(name)
                return (f'The {field.verbose_name} of this {self._meta.verbose_name} must be '
                        f'more than zero, not {value}.')
        return None

    def structural_changes(self) -> list[str]:
        """Names of the structural fields this unsaved state would change; [] if none."""
        if not self.STRUCTURAL_FIELDS or self._state.adding or self.pk is None:
            return []
        fields: list[Any] = [self._meta.get_field(name) for name in self.STRUCTURAL_FIELDS]
        stored = type(self)._default_manager.filter(pk=self.pk).values(
            '_creation_handled', *[field.attname for field in fields]).first()
        if stored is None or not stored['_creation_handled']:
            # Still being created: its own handlers set these (an allocation is re-pointed
            # at the parcel split off for it).
            return []
        return [
            field.name for field in fields
            if field.to_python(stored[field.attname]) != field.to_python(getattr(self, field.attname))
        ]

    def _structural_change_message(self, changed: list[str]) -> str:
        names = ', '.join(str(cast(Any, self._meta.get_field(name)).verbose_name) for name in changed)
        return (
            f'The {names} of this {self._meta.verbose_name} cannot be changed: other records '
            f'were worked out from it when it was entered, and nothing works them out again. '
            f'Delete it and enter it again.')

    def chronology_problem(self) -> str | None:
        """Why this new record would be applied out of date order, or None.

        Parcels are worked out event by event as records are entered, so an event dated
        before one already applied (a buy before a split) would miss it.
        """
        return None

    def _is_new_event(self) -> bool:
        # A row from an export arrives already handled, with what it derived beside it.
        return self._state.adding and not getattr(self, '_creation_handled', False)

    def clean(self) -> None:
        super().clean()
        changed = self.structural_changes()
        if changed:
            raise ValidationError(self._structural_change_message(changed))
        if self._is_new_event():
            problem = self.chronology_problem()
            if problem:
                raise ValidationError(problem)

    def save(self, *args: Any, **kwargs: Any) -> None:
        user = kwargs.pop('user', None)
        update_fields = kwargs.get('update_fields')
        if update_fields is None or set(update_fields) & set(self.STRUCTURAL_FIELDS):
            changed = self.structural_changes()
            if changed:
                raise ValueError(self._structural_change_message(changed))
        if self._state.adding:
            problem = self._not_positive_message()
            if problem:
                raise ValueError(problem)
        if self._is_new_event():
            problem = self.chronology_problem()
            if problem:
                raise ValueError(problem)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.description}'


class LogEntry(BaseModel):
    MODEL_DESCRIPTION = 'Log entries. Key events are recorded here.'
    event = models.CharField(max_length=255,
        help_text='What happened, in words.')
    notes = None # Don't want notes on logs
    # Generic Foreign Key fields
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name='logs', editable=False,
        help_text='The kind of record the event is about.')
    object_id = models.UUIDField(help_text='Identifier of the record the event is about.')
    content_object = GenericForeignKey('content_type', 'object_id')

    def __str__(self) -> str:
        return f'{self.created_at.isoformat(timespec="seconds")} - ***{(str(self.pk))[-4:]} - {self.event}'


class AbstractExchangeRate(models.Model): # Not using BaseModel as doesn't need description, notes, is_active, created_at etc
    MODEL_DESCRIPTION = 'Abstract base class for exchange rates between pairs of currencies'

    class Meta:
        abstract = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of the rate.')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=False,
        help_text='The portfolio the rate is stored for.')
    updated_at = models.DateTimeField(auto_now=True, editable=False,
        help_text='When the rate was last changed.')

    convert_to = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES,
        help_text='Currency converted into, such as AUD.')
    convert_from = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES,
        help_text='Currency converted from, such as USD.')
    exchange_rate_multiplier = models.DecimalField(max_digits=16, decimal_places=6, default=Decimal('1.0'),
        help_text='Amount of the "to" currency for one unit of the "from" currency.')

    def apply(self, money: Money) -> Money:
        assert str(money.currency) == str(self.convert_from), (
            f'Invalid exchange rate applied. The convert_from currency {self.convert_from} '
            f'does not match the currency {money.currency}'
        )

        new_amount = money.amount * self.exchange_rate_multiplier

        return Money(new_amount, str(self.convert_to))

    def update_current(self) -> 'CurrentExchangeRate | None':
        """Copy this rate to CurrentExchangeRate, unless a later-dated rate exists."""

        if hasattr(self, 'date'):
            newer_exists = type(self)._default_manager.filter(
                account=self.account,
                convert_from=self.convert_from,
                convert_to=self.convert_to,
                date__gt=self.date,  # type: ignore[misc]  # `date` is on the subclass
            ).exists()
            if newer_exists:
                return CurrentExchangeRate.objects.filter(
                    account=self.account,
                    convert_from=self.convert_from,
                    convert_to=self.convert_to,
                ).first()

        current, created = CurrentExchangeRate.objects.get_or_create(
            account=self.account,
            convert_from=self.convert_from,
            convert_to=self.convert_to,
            defaults={'exchange_rate_multiplier': self.exchange_rate_multiplier},
        )

        if not created:
            current.exchange_rate_multiplier = self.exchange_rate_multiplier
            current.save(update_fields=["exchange_rate_multiplier", "updated_at"])

        return current
    

    def __str__(self) -> str:

        exchange_rate_text = f'1 {self.convert_from} = {self.exchange_rate_multiplier} {self.convert_to}'

        if hasattr(self, 'date'):
            exchange_rate_text += f' on {self.date.isoformat()}'
        return exchange_rate_text
    

class CurrentExchangeRate(AbstractExchangeRate):
                           
    MODEL_DESCRIPTION = 'Exchange rates between pairs of currencies, latest date only.'           

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'convert_from', 'convert_to'], name='current_exchange_rate_keys')
        ]


    @classmethod
    def get_or_create(cls, account: 'Account', convert_from: str, convert_to: str,
                      force_refresh: bool = False) -> 'CurrentExchangeRate | None':
        """Get the current rate, fetching it if missing, over an hour old, or `force_refresh`.

        Keeps the stored rate if the fetch fails; returns None if there is none.
        """
        obj = cls.objects.filter(
            account=account,
            convert_from=convert_from,
            convert_to=convert_to,
        ).first()

        needs_refresh = (
            force_refresh
            or obj is None
            or (obj.updated_at < datetime.now(UTC) - timedelta(hours=1))
        )

        if needs_refresh:
            exchange_rate_multiplier = yfinanceinterface.get_exchange_rate(
                convert_from=convert_from, convert_to=convert_to, exchange_date=None
            )
            if exchange_rate_multiplier is not None:
                obj, _ = cls.objects.update_or_create(
                    account=account,
                    convert_from=convert_from,
                    convert_to=convert_to,
                    defaults={"exchange_rate_multiplier": exchange_rate_multiplier},
                )
            else:
                logger.warning(
                    "Could not fetch exchange rate for %s to %s; keeping existing rate if any.",
                    convert_from, convert_to,
                )
                if obj is None:
                    return None

        return obj


class ExchangeRate(AbstractExchangeRate):
    MODEL_DESCRIPTION = 'Exchange rates between pairs of currencies at particular dates.'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'convert_from', 'convert_to', 'date'], name='exchange_rate_keys')
        ]
        indexes = [
            models.Index(fields=['account', 'convert_from', 'convert_to', 'date'], name='exchange_rate_idx')
        ]
    
    date = models.DateField(help_text='Day the rate applies to.')
    is_continuous_history = models.BooleanField(default=False, editable=False,
        help_text='True when the rate came from a continuous run of daily history. Set by '
                  'the app; do not edit.')

    #: The rate could not be fetched, so this one stands in: the nearest known rate, or 1.0
    #: if there was none. Fetched again the next time it is asked for, and replaced by the
    #: history refresh, instead of being trusted for good.
    is_placeholder = models.BooleanField(default=False, editable=False,
        help_text='True when no quote existed for the day and the last earlier rate was '
                  'used instead. Set by the app; do not edit.')

    @classmethod
    def get_or_create(cls, account: 'Account', convert_from: str, convert_to: str,
                      exchange_date: Date) -> 'ExchangeRate':
        obj = cls.objects.filter(
            account=account,
            convert_from=convert_from,
            convert_to=convert_to,
            date=exchange_date
        ).first()
        if obj is not None and not obj.is_placeholder:
            return obj

        if obj is None:
            obj, _created = cls.objects.get_or_create(
                account=account,
                convert_from=convert_from,
                convert_to=convert_to,
                date=exchange_date,
                defaults={'exchange_rate_multiplier' : Decimal('1.0')}
            )

        field = cls._meta.get_field('exchange_rate_multiplier')
        fetched_rate = yfinanceinterface.get_exchange_rate(
            convert_from=convert_from,
            convert_to=convert_to,
            exchange_date=exchange_date,
        )
        if fetched_rate is not None:
            obj.replace_placeholder(convert_to_decimal_field(fetched_rate, field))
        elif obj.is_placeholder:
            logger.warning(
                "Still could not fetch exchange rate for %s to %s on %s; keeping the stand-in "
                "rate %s.", convert_from, convert_to, exchange_date, obj.exchange_rate_multiplier)
        elif convert_from != convert_to:
            # A failed cross-currency fetch must NOT keep the 1.0 default - that
            # relabels foreign amounts as base currency (e.g. USD shown as AUD).
            # Stand in the nearest known rate, earlier first, and mark it so the real
            # rate replaces it once it can be fetched.
            fallback = cls.nearest_known(account, convert_from, convert_to, exchange_date,
                                         exclude=obj.pk)
            obj.is_placeholder = True
            if fallback is not None:
                obj.exchange_rate_multiplier = fallback.exchange_rate_multiplier
                logger.warning(
                    "Could not fetch exchange rate for %s to %s on %s; standing in the "
                    "nearest known rate, from %s (%s).",
                    convert_from, convert_to, exchange_date,
                    fallback.date, fallback.exchange_rate_multiplier,
                )
            else:
                logger.error(
                    "Could not fetch exchange rate for %s to %s on %s and no other "
                    "rate exists; leaving multiplier at 1.0. Figures for this currency "
                    "will be unconverted until a rate is available.",
                    convert_from, convert_to, exchange_date,
                )
            obj.save(update_fields=['exchange_rate_multiplier', 'is_placeholder'])

        obj.update_current()
        return obj

    @classmethod
    def nearest_known(cls, account: 'Account', convert_from: str, convert_to: str, exchange_date: Date,
                      exclude: Any = None) -> 'ExchangeRate | None':
        """The fetched rate for the pair nearest `exchange_date`, preferring an earlier one."""
        known = cls.objects.filter(
            account=account, convert_from=convert_from, convert_to=convert_to,
            is_placeholder=False,
        ).exclude(pk=exclude)
        return (
            known.filter(date__lte=exchange_date).order_by('-date').first()
            or known.filter(date__gt=exchange_date).order_by('date').first()
        )

    def replace_placeholder(self, multiplier: Decimal | None) -> None:
        """Set the real rate, recalculating what was converted at the stand-in, if it was one."""
        was_placeholder = self.is_placeholder
        self.exchange_rate_multiplier = cast(Decimal, multiplier)
        self.is_placeholder = False
        self.save(update_fields=['exchange_rate_multiplier', 'is_placeholder'])
        if was_placeholder:
            from share_dinkum_app import recalculate
            recalculate.after_rate_change(self)

    def rate_corrected(self) -> None:
        """Settle a multiplier just changed by hand.

        It is a real rate now, so it stops being a stand-in that the next refresh fetches over,
        and whatever was converted at the old figure is worked out again.
        """
        if self.is_placeholder:
            self.is_placeholder = False
            self.save(update_fields=['is_placeholder'])
        self.update_current()
        from share_dinkum_app import recalculate
        recalculate.after_rate_change(self)

    @classmethod
    def update_exchange_rate_history(cls, account: 'Account', convert_from: str, convert_to: str) -> 'ExchangeRate | None':

        if convert_from == convert_to:
            return None
        
        start_date = None

        # Fetch the latest price history entry for the related instrument
        latest_continuous_exchange_rate = ExchangeRate.objects.filter(account=account, convert_from=convert_from, convert_to=convert_to, is_continuous_history=True).order_by('-date').first()
        if latest_continuous_exchange_rate:
            start_date = latest_continuous_exchange_rate.date

        if not start_date:
            earliest_buy = Buy.objects.filter(account=account).order_by('date').first()
            if earliest_buy:
                start_date = earliest_buy.date
            else:
                return None  # No buys, so no need to fetch exchange rates
            
        try:

            price_history = yfinanceinterface.get_exchange_rate_history(convert_from=convert_from, convert_to=convert_to, start_date=start_date)

            field = cls._meta.get_field('exchange_rate_multiplier')
            price_history['exchange_rate_multiplier'] = price_history['exchange_rate_multiplier'].apply(
                lambda val: convert_to_decimal_field(val, field)  # type: ignore[arg-type, return-value]
            )

            price_history['account'] = account  # type: ignore[call-overload]
            price_history['id'] = price_history['date'].apply(lambda x : uuid7())  # type: ignore[arg-type, return-value]
            
            # Bulk insert/update price history
            price_history_entries: list[ExchangeRate] = []
            for _, row in price_history.iterrows():
                price_history_entries.append(
                    ExchangeRate(  # type: ignore[arg-type]
                        **row.to_dict()
                    )
                )

            # Use bulk_create with `ignore_conflicts=True` to avoid duplicate errors
            with transaction.atomic():
                ExchangeRate.objects.bulk_create(price_history_entries, ignore_conflicts=True)

                # A conflict keeps the existing row, which is right for a fetched rate but not
                # for a stand-in: replace those with the history's rate for that day, or the
                # last trading day before it.
                history = sorted(zip(price_history['date'], price_history['exchange_rate_multiplier']))
                history_dates = [day for day, _ in history]
                for placeholder in ExchangeRate.objects.filter(
                        account=account, convert_from=convert_from, convert_to=convert_to,
                        is_placeholder=True):
                    index = bisect.bisect_right(history_dates, placeholder.date) - 1
                    if index >= 0 and (placeholder.date - history_dates[index]).days <= 7:
                        placeholder.replace_placeholder(history[index][1])

            if not price_history.empty:
                latest_row = price_history.loc[price_history['date'].idxmax()]  # type: ignore[call-overload]
                latest_multiplier = convert_to_decimal_field(
                    latest_row['exchange_rate_multiplier'],
                    field
                )
                latest = ExchangeRate(
                    id=latest_row['id'],
                    account=latest_row['account'],
                    convert_from=latest_row['convert_from'],
                    convert_to=latest_row['convert_to'],
                    date=latest_row['date'],
                    exchange_rate_multiplier=cast(Decimal, latest_multiplier)
                )
                latest.update_current()
                return latest


        except Exception as e:
            logger.error(f'Error getting exchange rate history for {convert_from} to {convert_to}, {e}', exc_info=True)
        return None



class Market(BaseModel):
    MODEL_DESCRIPTION = 'Share markets, such as ASX, NASDAQ, LSE, etc'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'code'], name='market_keys')
        ]

    code = models.CharField(max_length=16,
        help_text='Short code of the market, such as ASX or NASDAQ.')

    suffix = models.CharField(max_length=16, null=True, blank=True,
        help_text="Suffix the price provider adds to this market's tickers, such as .AX. "
                  'Blank if none.')

    # Where the market is, which decides whether the assets listed on it count as
    # "Australian listed" on the CGT schedule. Left blank rather than assumed; the country
    # is suggested from the suffix or the code when a market is created.
    country = models.CharField(
        max_length=2, null=True, blank=True,
        help_text='ISO country code, e.g. AU. Decides whether instruments here are treated '
                  'as Australian listed for capital gains reporting.',
    )
    is_exchange_listed = models.BooleanField(
        default=True,
        help_text='Uncheck for unlisted holdings. Unlisted assets fall into the "other" '
                  'categories on the CGT schedule regardless of country.',
    )


class Instrument(BaseModel):
    MODEL_DESCRIPTION = 'Share codes, eg BHP, VGS, VAS, etc'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['name', 'account'], name='instrument_keys')
        ]

    # What the thing legally *is*, which the CGT schedule cares about and the ticker does
    # not reveal. AFI and VAS are both ASX listed and AUD quoted; one is a company and the
    # other a unit trust, and they belong in different boxes on the form.


    name = models.CharField(max_length=16,
        help_text='Ticker code without the market suffix, such as BHP or VGS. Unique within '
                  'a portfolio.')
    description = models.CharField(max_length=255, blank=True,
        help_text='Full name of the security.')
    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES,
        help_text='Currency the instrument trades in, such as AUD or USD.')
    market = models.ForeignKey(Market, on_delete=models.PROTECT,
        help_text='The market the instrument is listed on. Enter its code as listed in the '
                  'Market table.')
    current_unit_price = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True,
        help_text='Latest price per unit, refreshed from market data. Set by the app; do '
                  'not edit.')

    legal_form = models.CharField(
        max_length=13, choices=LegalForm.choices, default=LegalForm.UNKNOWN,
        help_text='Shown on the product disclosure statement or annual tax statement.',
    )
    #: Where the legal form came from; set automatically, see `save`. Only USER counts as
    #: confirmed, and schedules on unconfirmed instruments are drafts.
    legal_form_source = models.CharField(
        max_length=9, choices=LegalFormSource.choices, default=LegalFormSource.DEFAULT,
        editable=False,
        help_text='Where the instrument\'s legal form came from. Only "entered by the user" '
                  'counts as confirmed. Set by the app; do not edit.',
    )
    #: Overrides the derived schedule category. Limited to the schedule's own categories.
    cgt_asset_category_override = models.CharField(
        max_length=48, null=True, blank=True,
        choices=CGTAssetCategory.reportable_choices(),
        help_text='Leave empty. Only set this if the category worked out from the legal '
                  'form and the market is wrong for this holding.',
    )
    #: Overrides the per-parcel TAP derivation for every parcel. Leave empty normally:
    #: "no" also switches off the s104-165(3) deeming on departure.
    is_taxable_australian_property_override = models.BooleanField(
        null=True, blank=True,
        help_text='Leave empty. Only set this if the instrument is taxable Australian '
                  'property in its own right -- real property, or a non-portfolio interest '
                  'in a land rich entity. Setting it to "no" is not the same as leaving it '
                  'empty: it overrides the departure deeming for every parcel, including '
                  'ones you held when you ceased Australian residency.',
    )

    @safe_property
    def cgt_asset_category(self) -> str:
        """Which box on the CGT schedule a gain on this instrument belongs in."""
        from share_dinkum_app.cgt import classification
        return classification.asset_category(self)

    @safe_property
    def is_classified(self) -> bool:
        """Whether the legal form is known and was set or confirmed by the user, not suggested."""
        return (self.legal_form != LegalForm.UNKNOWN
                and self.legal_form_source == LegalFormSource.USER)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Set `legal_form_source` to USER when the legal form is set by hand.

        That is: created with a legal form and a DEFAULT source, or the legal form changed
        without the caller also changing the source (the suggester sets its own).
        """
        if self.legal_form != LegalForm.UNKNOWN:
            previous = (
                Instrument.objects.filter(pk=self.pk)
                .values('legal_form', 'legal_form_source').first()
                if self.pk else None
            )
            if previous is None:
                # Created already carrying a legal form, so it came from a person or a file
                # rather than from the default.
                if self.legal_form_source == LegalFormSource.DEFAULT:
                    self.legal_form_source = LegalFormSource.USER
            else:
                changed = previous['legal_form'] != self.legal_form
                source_set_by_caller = (
                    previous['legal_form_source'] != self.legal_form_source)
                if changed and not source_set_by_caller:
                    self.legal_form_source = LegalFormSource.USER

            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'legal_form' in set(update_fields):
                kwargs['update_fields'] = set(update_fields) | {'legal_form_source'}

        super().save(*args, **kwargs)

    calculated_quantity_held = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True, editable=False,
        help_text='Units currently held, across all unsold parcels. Set by the app; do not '
                  'edit.')
    

    @safe_property
    def quantity_held(self) -> Decimal:
        # Remaining quantity is a bit more complicated than just buys minus sells, since parcels can be split, consolidated and bifurcated.
        parcels = Parcel.objects.filter(
            account=self.account,
            buy__instrument=self,
            deactivation_date__isnull=True
        ).annotate(
            allocated=Coalesce(
                Sum(
                    'sale_allocation__quantity',
                    filter=Q(sale_allocation__is_active=True)
                ),
                Decimal('0')
            )
        )

        return parcels.aggregate(
            total=Sum(F('parcel_quantity') - F('allocated'))
        )['total'] or Decimal('0')

    calculated_value_held =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text="Value of the units currently held, in the instrument's currency. Set by "
                  'the app; do not edit.')
    
    @safe_property
    def value_held(self) -> Money:
        if self.current_unit_price:
            value_held = Money(self.current_unit_price * self.quantity_held, self.currency)
        else:
            if not self.currency:
                raise ValueError(f'Instrument {self} has no currency set.')
            else:
                value_held = Money(0, self.currency)
        return value_held

    calculated_value_held_converted =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Value of the units currently held, in the portfolio currency. Set by the '
                  'app; do not edit.')
    
    @safe_property
    def value_held_converted(self) -> Money | None:
        
        if self.currency == self.account.currency:
            # Already in account currency
            assert isinstance(self.value_held, Money), f'Value held is not a Money instance: {self.value_held}'
            return self.value_held

        # Get or refresh the current exchange rate
        current_rate = CurrentExchangeRate.get_or_create(
            account=self.account,
            convert_from=self.currency,
            convert_to=self.account.currency,
        )

        if not current_rate:
            # Unknown rather than an error: raising here failed every save of the instrument,
            # and so every trade in it, whenever the rate could not be fetched (offline).
            logger.error(
                'No exchange rate available for %s to %s, so the value of %s in %s is unknown '
                'until one can be fetched.', self.currency, self.account.currency, self,
                self.account.currency)
            return None

        converted_value = current_rate.apply(self.value_held)
        assert isinstance(converted_value, Money), f'Converted value held is not a Money instance: {converted_value}'
        return converted_value


    @safe_property
    def yfinance_ticker_code(self) -> str:
        
        suffix = self.market.suffix
        
        if suffix:
            suffix = suffix.replace('.', '')
            return f'{self.name}.{suffix}'
        else:
            return self.name

    def __str__(self) -> str:
        if self.is_active:
            return f'{self.name} - {self.description} [{self.account.description}]'
        else:
            return f'{self.name} - {self.description} (INACTIVE)'

    #: Stored prices are replaced by what is fetched for the same day, not kept.
    PRICE_HISTORY_FIELDS: list[str] = ['open', 'high', 'low', 'close', 'volume', 'stock_splits']

    def update_price_history(self, end_date: Date | None = None, start_date: Date | None = None) -> int:
        """Fetch price history up to `end_date` (default today) and update the current price.

        Starts at `start_date` if given, else four days before the latest stored price, to
        cover weekends and suspensions, or from the first buy if there is none. A day already
        stored is overwritten: a price fetched while the market was open is not that day's
        close, and one stored adjusted is not the price it traded at. Returns the number of
        days stored.
        """
        end_date = end_date or date.today()

        latest_price_history = (
            InstrumentPriceHistory.objects.filter(instrument=self)
            .order_by('-date')
            .first()
        )

        if start_date is None and latest_price_history:
            start_date = latest_price_history.date - timedelta(days=4)
            if start_date > end_date:
                start_date = end_date
        elif start_date is None:
            earliest_buy = (
                Buy.objects.filter(instrument=self)
                .order_by('date')
                .first()
            )
            if earliest_buy:
                start_date = earliest_buy.date
            else:
                start_date = date(2020, 1, 1)

        if start_date > end_date:
            return 0

        try:
            price_history = yfinanceinterface.get_instrument_price_history(
                instrument=self,
                start_date=start_date,
                end_date=end_date,
            )
            if price_history.empty:
                logger.warning('No price history returned for %s between %s and %s', self, start_date, end_date)
                return 0

            decimal_fields = {
                field_name: InstrumentPriceHistory._meta.get_field(field_name)
                for field_name in ['open', 'high', 'low', 'close', 'stock_splits']
            }
            for column, field in decimal_fields.items():
                price_history[column] = price_history[column].apply(
                    lambda val: convert_to_decimal_field(val, field)  # type: ignore[arg-type, return-value]
                )

            price_history['account'] = self.account  # type: ignore[call-overload]
            price_history['id'] = price_history['date'].apply(lambda x: uuid7())  # type: ignore[arg-type, return-value]

            # Drop rows with null close prices (yfinance sometimes returns NaN for recent dates)
            price_history = price_history.dropna(subset=['close'])
            if price_history.empty:
                logger.warning('No valid close prices for %s between %s and %s', self, start_date, end_date)
                return 0

            instrument_price_field = self._meta.get_field('current_unit_price')

            # Try current price first, fall back to last valid close
            current_price = yfinanceinterface.get_current_price(self)
            if current_price is not None:
                self.current_unit_price = convert_to_decimal_field(current_price, instrument_price_field)
            elif not price_history.empty:
                self.current_unit_price = convert_to_decimal_field(
                    price_history['close'].iloc[-1],
                    instrument_price_field
                )
            self.save()

            price_history_entries: list[InstrumentPriceHistory] = []
            for _, row in price_history.iterrows():
                price_history_entries.append(
                    InstrumentPriceHistory(  # type: ignore[arg-type]
                        **row.to_dict()
                    )
                )

            with transaction.atomic():
                InstrumentPriceHistory.objects.bulk_create(
                    price_history_entries,
                    update_conflicts=True,
                    unique_fields=['account', 'instrument', 'date'],
                    update_fields=self.PRICE_HISTORY_FIELDS,
                )
            return len(price_history_entries)

        except Exception as e:
            logger.error(f'Error getting price history for {self} between {start_date} and {end_date}, {e}', exc_info=True)
            return 0



class InstrumentPriceHistory(models.Model):
    MODEL_DESCRIPTION = 'Price history for instruments.'
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'instrument', 'date'], name='instrument_price_history_keys')
        ]
        indexes = [
            models.Index(fields=['account', 'instrument', 'date'], name='instrument_date_idx')
        ]
        ordering = ['date'] 

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of the price row.')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=False,
        help_text='The portfolio the price is stored for.')
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE,  editable=False,
        help_text='The instrument priced.')
    date = models.DateField(editable=False,
        help_text='Trading day the prices are for.')
    open = models.DecimalField(max_digits=16, decimal_places=6, editable=False,
        help_text='Opening price, as traded that day (not adjusted for later splits).')
    high = models.DecimalField(max_digits=16, decimal_places=6, editable=False,
        help_text='Highest price, as traded that day (not adjusted for later splits).')
    low = models.DecimalField(max_digits=16, decimal_places=6, editable=False,
        help_text='Lowest price, as traded that day (not adjusted for later splits).')
    close = models.DecimalField(max_digits=16, decimal_places=6, editable=False,
        help_text='Closing price, as traded that day (not adjusted for later splits).')
    volume = models.BigIntegerField(editable=False,
        help_text='Number of units traded that day.')
    stock_splits = models.DecimalField(max_digits=16, decimal_places=6, editable=False,
        help_text='Split ratio taking effect that day, such as 2 for a 2-for-1 split. 0 if '
                  'none.')


    def get_absolute_url(self) -> str:
        # Redirect stuff to admin
        app_label = self._meta.app_label
        model_name = self._meta.model_name
        return reverse(f'admin:{app_label}_{model_name}_change', args=[str(self.id)])


class Trade(BaseModel):
    MODEL_DESCRIPTION = 'A base class for trades, such as buys and sells.'

    class Meta:
        abstract = True

    description = models.CharField(max_length=255, null=True, blank=True, editable=False,
        help_text='Summary of the trade. Set by the app; do not edit.') # Setting this automatically
    instrument = models.ForeignKey(Instrument, related_name='%(class)s', on_delete=models.PROTECT,
        help_text='The instrument traded. Enter its name as listed in the Instrument table.')
    date = models.DateField(help_text='Date of the trade.')
    quantity = models.DecimalField(max_digits=16, decimal_places=4, validators=[validate_positive],
        help_text='Number of units traded. Must be positive.')
    unit_price = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        help_text='Price per unit, in the currency named beside it.')
    total_brokerage = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        help_text='Brokerage and fees for the whole trade, not per unit.')
    exchange_rate = models.ForeignKey(ExchangeRate, related_name='%(class)s', on_delete=models.PROTECT, blank=True, null=True,
        help_text='Rate to the portfolio currency on the trade date. Looked up for foreign '
                  'currency trades.')
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path,
        help_text='Path to a supporting document, such as a contract note. The file is '
                  'copied into the app; a path that does not exist is ignored.')

    # Calculated fields
    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
        help_text='Fiscal year the trade date falls in. Set by the app; do not edit.')
    
    @safe_property
    def fiscal_year(self) -> 'FiscalYear':
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.date)
        return fiscal_year
    
    calculated_total_brokerage_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Total brokerage in the portfolio currency. Set by the app; do not edit.')
    
    def rate_for(self, currency: str) -> 'ExchangeRate | None':
        """The rate converting `currency` to the portfolio currency on the trade date.

        None for the portfolio currency itself. The attached rate covers the currency of the
        price (or of the brokerage, if the price is zero). Brokerage charged in another
        currency, such as USD brokerage on a GBP trade, is converted at that currency's own
        rate for the day: a stored one if there is one, as this is read by every report,
        otherwise fetched and stored. `recalculate.after_rate_change` finds trades converted
        at it this way, as they are not linked to it.
        """
        account_currency = str(self.account.currency)
        if currency == account_currency:
            return None
        rate = self.exchange_rate
        if rate is not None and str(rate.convert_from) == currency:
            return rate
        stored = ExchangeRate.objects.filter(
            account=self.account, convert_from=currency, convert_to=account_currency,
            date=self.date).first()
        return stored or ExchangeRate.get_or_create(
            account=self.account, convert_from=currency, convert_to=account_currency,
            exchange_date=self.date)

    def convert(self, money: Money) -> Money:
        """`money` in the portfolio currency, at its own currency's rate on the trade date."""
        if not money.amount:
            # No rate is needed for nothing, and an unused brokerage column keeps its default
            # currency whatever the trade is in.
            return Money(Decimal('0'), str(self.account.currency))
        rate = self.rate_for(str(money.currency))
        return money if rate is None else rate.apply(money)

    @safe_property
    def total_brokerage_converted(self) -> Money:
        return self.convert(self.total_brokerage)
    
    calculated_unit_brokerage_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Brokerage per unit in the portfolio currency. Set by the app; do not '
                  'edit.')
    
    @safe_property
    def unit_brokerage_converted(self) -> Money:
        return self.convert(self.total_brokerage / self.quantity)
    
    calculated_unit_price_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Price per unit in the portfolio currency. Set by the app; do not edit.')
    
    @safe_property
    def unit_price_converted(self) -> Money:
        return self.convert(self.unit_price)

    @classmethod
    def converted_at(cls, rate: 'ExchangeRate') -> 'QuerySet[Any]':
        """Trades with an amount converted at `rate`: attached to it, or with a second
        currency it covers on its date."""
        if str(rate.convert_to) != str(rate.account.currency):
            return cls._default_manager.filter(exchange_rate=rate)
        on_its_date = Q(account=rate.account, date=rate.date) & (
            Q(unit_price_currency=rate.convert_from)
            | Q(total_brokerage_currency=rate.convert_from))
        return cls._default_manager.filter(Q(exchange_rate=rate) | on_its_date).distinct()

    def __str__(self) -> str:
        return f'{self.description}'

    STRUCTURAL_FIELDS: tuple[str, ...] = (
        'instrument', 'date', 'quantity', 'unit_price_currency', 'total_brokerage_currency')
    POSITIVE_FIELDS: tuple[str, ...] = ('quantity',)

    #: Can be corrected after the trade is entered. A change is carried to its parcels and
    #: allocations, whose stored figures would otherwise keep the old price.
    REPRICING_FIELDS: tuple[str, ...] = ('unit_price', 'total_brokerage', 'exchange_rate')

    def _repriced(self, update_fields: Any) -> bool:
        if self._state.adding or self.pk is None:
            return False
        if update_fields is not None and not set(update_fields) & set(self.REPRICING_FIELDS):
            return False
        fields: list[Any] = [self._meta.get_field(name) for name in self.REPRICING_FIELDS]
        stored = type(self)._default_manager.filter(pk=self.pk).values(
            '_creation_handled', *[field.attname for field in fields]).first()
        if stored is None or not stored['_creation_handled']:
            return False
        return any(
            field.to_python(stored[field.attname]) != field.to_python(getattr(self, field.attname))
            for field in fields)

    def save(self, *args: Any, **kwargs: Any) -> None:
        repriced = self._repriced(kwargs.get('update_fields'))
        if self.is_active:
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name} | {self.quantity} unit @ {self.unit_price} / unit'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)
        if repriced:
            from share_dinkum_app import recalculate
            recalculate.derived_from(cast('Buy | Sell', self))


class Buy(Trade):
    MODEL_DESCRIPTION = 'Purchases of share parcels.'

    _creation_handled = models.BooleanField(default=False, editable=False,
        help_text='Whether the app has already created the parcel for this buy. Set by the '
                  'app; do not edit.')

    calculated_related_parcels = models.TextField(null=True, blank=True, editable=False,
        help_text='The parcels created from this buy, as text. Set by the app; do not edit.')
    
    @safe_property
    def related_parcels(self) -> str:
        related_parcels = Parcel.objects.filter(buy=self)
        parcel_list ='\n'.join([str(parcel) for parcel in related_parcels])
        return parcel_list

    def chronology_problem(self) -> str | None:
        if not self.instrument_id or not self.date:
            return None
        # A split is dated on its ex-date and reaches buys before it. A buy on the ex-date is
        # already in post-split units, as a sale that day is, so only a later split matters.
        split = ShareSplit.objects.filter(
            account_id=self.account_id, instrument_id=self.instrument_id,
            _creation_handled=True, date__gt=self.date,
        ).order_by('date').first()
        if split is not None:
            return (
                f'A split of {self.instrument.name} on {split.date} has already been applied, '
                f'and this buy is dated before it, so it would not be split with the rest. '
                f'Delete the split, enter this buy, then enter the split again.')
        # An adjustment is spread over the parcels held in its year once, when entered. A
        # buy held on any day of that year, the year end included, would get none of it.
        adjustment = CostBaseAdjustment.spread_on_or_after(self, self.date)
        if adjustment is not None:
            return (
                f'A cost base adjustment of {self.instrument.name} for the year ending '
                f'{adjustment.financial_year_end_date} has already been spread over the parcels '
                f'held, and this buy is dated before that year end, so it would get none of it. '
                f'Delete the adjustment, enter this buy, then enter the adjustment again.')
        return None


class Sell(Trade):
    MODEL_DESCRIPTION = 'Sales of shares.'
    _creation_handled = models.BooleanField(default=False, editable=False,
        help_text='Whether the app has already allocated this sale to parcels. Set by the '
                  'app; do not edit.')

    
    strategy = models.CharField(
        max_length=7,
        choices=SellStrategy.choices,
        default=SellStrategy.MIN_CGT,
        help_text='How the sale picks the parcels it consumes. MANUAL means you give the '
                  'allocations yourself.',
    )

    STRUCTURAL_FIELDS: tuple[str, ...] = Trade.STRUCTURAL_FIELDS + ('strategy',)

    calculated_proceeds = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Quantity times unit price less brokerage, in the portfolio currency. Set '
                  'by the app; do not edit.')
    
    @safe_property
    def proceeds(self) -> Money:
        proceeds = (self.quantity * self.unit_price_converted) - self.total_brokerage_converted
        return proceeds
    
    calculated_unit_proceeds = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Proceeds per unit, in the portfolio currency. Set by the app; do not '
                  'edit.')
    
    @safe_property
    def unit_proceeds(self) -> Money:
        return self.proceeds / self.quantity

    calculated_unallocated_quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Units sold that no parcel has been allocated to yet. Set by the app; do '
                  'not edit.')
    
    @safe_property
    def unallocated_quantity(self) -> Decimal:
        allocated_quantity = self.sale_allocation.filter(is_active=True).aggregate(total_allocated=Sum('quantity'))['total_allocated'] or 0
        return cast(Decimal, (self.quantity or 0) - allocated_quantity)

    @classmethod
    def with_unallocated_quantity(cls, account: 'Account') -> 'QuerySet[Any]':  # annotated with `allocated`
        """Sales with units no parcel was allocated to, annotated with `allocated`.

        Gains are worked out per allocation, so those units are in no gain at all: a sale
        larger than the holding, one dated before its purchase, or a MANUAL sale never
        allocated.
        """
        return (
            cls.objects.filter(account=account, is_active=True)
            .annotate(allocated=Coalesce(
                Sum('sale_allocation__quantity', filter=Q(sale_allocation__is_active=True)),
                Decimal('0')))
            .filter(quantity__gt=F('allocated'))
            .select_related('instrument')
            .order_by('date')
        )

    def chronology_problem(self) -> str | None:
        if not self.instrument_id or not self.date:
            return None
        # A sale on a split's own date is in post-split units, so only a later split matters.
        split = ShareSplit.objects.filter(
            account_id=self.account_id, instrument_id=self.instrument_id,
            _creation_handled=True, date__gt=self.date, affected_parcels__isnull=False,
        ).order_by('date').first()
        if split is not None:
            return (
                f'A split of {self.instrument.name} on {split.date} has already been applied, '
                f'and this sale is dated before it, so it would be matched against the split '
                f'parcels. Delete the split, enter this sale, then enter the split again.')
        # A sale shortens the days its units were held, which weights the spread of any
        # adjustment for a year it falls before the end of. A sale on the year end changes
        # no weight, since the last day still counts as held.
        adjustment = CostBaseAdjustment.spread_on_or_after(
            self, self.date + timedelta(days=1), allocated=True)
        if adjustment is not None:
            return (
                f'A cost base adjustment of {self.instrument.name} for the year ending '
                f'{adjustment.financial_year_end_date} has already been spread over the parcels '
                f'held, and this sale is dated before that year end, so the units sold would '
                f'keep a share worked out as if still held. Delete the adjustment, enter this '
                f'sale, then enter the adjustment again.')
        return None


class Parcel(BaseModel):
    MODEL_DESCRIPTION = 'Collections of shares with the same unit properties. Can be split into other parcels.'
    description = models.CharField(max_length=255, null=True, blank=True, editable=False,
        help_text='Summary of the parcel. Set by the app; do not edit.') # Setting this automatically

    buy = models.ForeignKey(Buy, related_name='parcels', on_delete=models.CASCADE, editable=False,
        help_text='The buy this parcel came from.')
    parent_parcel = models.ForeignKey(
        'self',
        related_name='children',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        editable=False,
        help_text='The parcel this one was split from, by a sale or a share split. Blank '
                  'for the first.'
        )
    parcel_quantity = models.DecimalField(max_digits=16, decimal_places=4, editable=False,
        help_text='Units in the parcel, in the units in force today.')
    #: Ten places, since a consolidation's ratio rarely terminates: at four, 1-for-3 stored
    #: 0.3333 and every unit price divided by it came out 0.01% high.
    cumulative_split_multiplier = models.DecimalField(max_digits=22, decimal_places=10, editable=False, default=Decimal('1.0'),
        help_text='Product of every share split applied since the buy. Prices on the buy '
                  'are divided by it.')
    activation_date = models.DateField(null=True, editable=False,
        help_text='Date the parcel came into existence.')
    deactivation_date = models.DateField(null=True, editable=False,
        help_text='Date the parcel was replaced by split parcels. Blank while it is '
                  'current.')
    sale_date = models.DateField(null=True, editable=False,
        help_text='Date the parcel was sold. Blank while held.')

    calculated_instrument_name = models.CharField(max_length=16, null=True, blank=True, editable=False,
        help_text='Name of the instrument bought. Set by the app; do not edit.')
    
    @safe_property
    def instrument_name(self) -> str | None:
        return self.buy.instrument.name if self.buy and self.buy.instrument else None

    calculated_remaining_quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, editable=False,
        help_text='Units not yet sold. Set by the app; do not edit.')

    @safe_property
    def remaining_quantity(self) -> Decimal:

        if not self.is_active:
            return Decimal('0')
        
        sold_quantity = self.sale_allocation.filter(is_active=True).aggregate(total_allocated=Sum('quantity'))['total_allocated'] or 0
        return self.parcel_quantity - sold_quantity

    calculated_is_sold = models.BooleanField(null=True, blank=True, editable=False,
        help_text='True once every unit has been sold. Set by the app; do not edit.')
    
    @safe_property
    def is_sold(self) -> bool:
        return self.remaining_quantity <= Decimal('0') # Using <= to account for any potential rounding issues


    @safe_property
    def adjusted_buy_price(self) -> Money:
        adjusted_buy_price = self.buy.unit_price_converted / self.cumulative_split_multiplier
        return adjusted_buy_price

    calculated_adjusted_unit_brokerage = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Buy brokerage per unit after splits, in the portfolio currency. Set by '
                  'the app; do not edit.')
    
    @safe_property
    def adjusted_unit_brokerage(self) -> Money:

        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        adjusted_unit_brokerage = self.buy.unit_brokerage_converted / self.cumulative_split_multiplier
        return adjusted_unit_brokerage

    
    @safe_property
    def total_adjustments(self) -> Money:
        total_adjustment = self.cost_base_adjustment_allocation.filter(
            deactivation_date__isnull=True
        ).aggregate(
            total=Sum('cost_base_increase')
        )['total'] or Decimal('0')

        total_adjustment = Money(total_adjustment, self.buy.account.currency)  # TODO assumes all in base currency
        return total_adjustment
    
    calculated_total_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Price, brokerage and cost base adjustments for the whole parcel, in the '
                  'portfolio currency. Set by the app; do not edit.')
    
    @safe_property
    def total_cost_base(self) -> Money:

        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        # Already converted
        parcel_quantity = self.parcel_quantity
        total_cost_base = (self.adjusted_buy_price * parcel_quantity)
        total_cost_base += (self.adjusted_unit_brokerage * parcel_quantity)
        total_cost_base = add_currencies(total_cost_base, self.total_adjustments,
                                         default_currency=str(self.buy.account.currency))

        return total_cost_base
    
    calculated_unit_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Total cost base divided by the units in the parcel. Set by the app; do '
                  'not edit.')
    
    @safe_property
    def unit_cost_base(self) -> Money:
        
        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        return self.total_cost_base / self.parcel_quantity

    @classmethod
    def with_unconverted_cost_base(cls, account: 'Account') -> 'QuerySet[Parcel]':
        """Active parcels whose stored cost base is not in the account's currency.

        Left by a bug fixed in 0.3.0: a parcel was built before its foreign-currency buy had
        an exchange rate. Reports read the live figures and were right; only the stored copy
        was wrong. Saving the parcel again recalculates it. A zero cost base is ignored,
        since its currency changes nothing.
        """
        return (
            cls.objects.filter(account=account, deactivation_date__isnull=True)
            .exclude(calculated_total_cost_base_currency=str(account.currency))
            .exclude(calculated_total_cost_base=Decimal('0'))
        )

    def market_value_at(self, day: Date, purpose: str = 'CUTOVER_2027') -> tuple[Money | None, str | None]:
        """This parcel's market value on `day`, for a deemed disposal.

        Worked out from the instrument's per-unit valuation, adjusted for later splits, so it
        survives the parcel being split or bifurcated. Returns `(value, source)`, or
        `(None, None)` if there is no valuation.
        """
        from share_dinkum_app.cgt import cutover

        unit_value, source = cutover.unit_value_at(
            self.buy.instrument, day, purpose=purpose)
        if unit_value is None:
            return None, None

        # A valuation is per unit as units stood on the valuation date. Any split since has
        # multiplied the unit count and divided the value, so the recorded figure has to be
        # brought forward to today's units before it is multiplied out.
        multiplier = cutover.scale_for_splits(self, day)
        value = (unit_value / multiplier) * self.parcel_quantity

        # Price history is quoted in the instrument's currency, and a recorded valuation may
        # be too. The cost base it is compared with is in the account's.
        account_currency = str(self.account.currency)
        if str(value.currency) != account_currency:
            rate = ExchangeRate.get_or_create(
                account=self.account, convert_from=str(value.currency),
                convert_to=account_currency, exchange_date=day)
            value = rate.apply(value)
        return value, source

    def split_or_consolidate(self, multiplier: Decimal, date: Date) -> 'Parcel':
        """Replace this parcel with one of `multiplier` times the units, carrying its adjustments.

        Pass the exact ratio (`ShareSplit.ratio`), not the rounded `split_multiplier`: a
        1-for-3 consolidation of 3,000 units then gives 1,000 rather than 999.999.
        """
        assert multiplier > 0
        assert self.is_active

        new_parcel_message = f'This parcel was created by splitting parcel {self.pk} by multiplier {multiplier:.6g}'

        with transaction.atomic():
            # Create target parcel
            parcel_target = copy.copy(self) # create a shallow copy
            parcel_target.pk = None # Make a new instance
            parcel_target.activation_date = date # Set new activation date
            parcel_target.parent_parcel = self
            parcel_target.parcel_quantity = cast(Decimal, convert_to_decimal_field(
                self.parcel_quantity * multiplier, self._meta.get_field('parcel_quantity')))
            parcel_target.cumulative_split_multiplier = cast(Decimal, convert_to_decimal_field(
                self.cumulative_split_multiplier * multiplier,
                self._meta.get_field('cumulative_split_multiplier')))
            parcel_target.save()
            parcel_target.log_event(new_parcel_message)

            # The adjustments go with the units. Left behind on this parcel, which is about to
            # be deactivated, they would drop out of the cost base without a trace.
            for allocation in self.cost_base_adjustment_allocation.filter(is_active=True):
                allocation.move_to(parcel_target, date=date)

            # Update old parcel
            self.log_event(f'This parcel was split with multipler {multiplier:.6g}, then marked as INACTIVE. New parcel is {parcel_target.pk}.')

            # This sets is_active = False for the old parcel
            self.deactivation_date = date

            self.save() # not needed as add_note also saves

        return parcel_target

    def bifurcate(self, quantity: Decimal, date: Date) -> 'Parcel':
        # Errors rather than asserts: they name the parcel, and survive `python -O`.
        if quantity <= 0:
            raise ValueError(f'Cannot split {quantity} units off parcel {self.pk}: the quantity must be more than zero.')
        if quantity > self.parcel_quantity:
            raise ValueError(
                f'Cannot split {quantity} units off parcel {self.pk} (bought {self.buy.date}): '
                f'it only holds {self.parcel_quantity}.')
        if not self.is_active:
            raise ValueError(f'Cannot split parcel {self.pk}: it was replaced on {self.deactivation_date}.')

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
            parcel_remainder.activation_date = date # Set new activation date
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
            

            for adjustment in related_adjustments:
                adjustment.bifurcate(
                    target_parcel=parcel_target,
                    remainder_parcel=parcel_remainder,
                    date=date
                    )

        return parcel_target

    def __str__(self) -> str:
        if self.is_active:
            parcel_desc  = f'{self.description} @ {self.adjusted_buy_price} / unit | Total cost base = {self.total_cost_base} |'
            if self.is_sold:
                parcel_desc  += ' SOLD'
            return parcel_desc 
        else:
            return f'{self.pk} | INACTIVE'

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.is_active = self.deactivation_date is None
        self.description = f'{self.buy.date} | PARCEL |  {self.buy.instrument.name} | {self.parcel_quantity} unit'
        super().save(*args, **kwargs)


class SellAllocation(BaseModel):
    MODEL_DESCRIPTION = 'Allocations of sell events to specific parcels.'
    description = models.CharField(max_length=255, null=True, blank=True, editable=False,
        help_text='Summary of the allocation. Set by the app; do not edit.') # Setting this automatically

    _creation_handled = models.BooleanField(default=False, editable=False,
        help_text='Whether the app has already split the parcel for this allocation. Set by '
                  'the app; do not edit.')

    parcel = models.ForeignKey(Parcel, related_name='sale_allocation', on_delete=models.PROTECT,
        help_text='The parcel the units are taken from.')
    sell = models.ForeignKey(Sell, related_name='sale_allocation', on_delete=models.PROTECT,
        help_text='The sale the units are allocated to.')
    quantity = models.DecimalField(max_digits=16, decimal_places=4,
        help_text='Units taken from the parcel by the sale.')

    STRUCTURAL_FIELDS: tuple[str, ...] = ('parcel', 'sell', 'quantity')

    calculated_sale_date = models.DateField(null=True, blank=True, editable=False,
        help_text='Date of the sale. Set by the app; do not edit.')
    
    @safe_property
    def sale_date(self) -> Date:
        return self.sell.date
    
    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
        help_text='Fiscal year of the sale. Set by the app; do not edit.')
    
    @safe_property
    def fiscal_year(self) -> 'FiscalYear':
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.sale_date)
        return fiscal_year
    
    calculated_days_held = models.IntegerField(null=True, blank=True, editable=False,
        help_text='Days from the buy to the sale. Set by the app; do not edit.')
    
    @safe_property
    def days_held(self) -> int:
        return (self.sell.date - self.parcel.buy.date).days
    
    calculated_total_capital_gain = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text="Share of the sale proceeds less the parcel's cost base, in the portfolio "
                  'currency. Before any CGT discount. Set by the app; do not edit.')
    
    @safe_property
    def total_capital_gain(self) -> Money:
        # Note, a parcel is always fully consumed by a sell allocation due to the bifurcation process, therefore can just use parcel.total_cost_base rather than unit cost base and qty. This avoids rounding issues
        return (self.sell.proceeds * self.quantity / self.sell.quantity) - self.parcel.total_cost_base

    def allocation_problems(self) -> list[str]:
        """Why this new allocation cannot be made, as messages; empty if it can.

        Checked before saving, since saving splits the parcel. Without it a sold parcel
        could be sold again, and the holding would go negative.
        """
        if not self.parcel_id or not self.sell_id or self.quantity is None:
            return []

        def units(value: Any) -> str:
            return f'{Decimal(value).normalize():f}'

        parcel, sell = self.parcel, self.sell
        buy = parcel.buy
        problems: list[str] = []
        if self.quantity <= 0:
            problems.append(f'The quantity must be more than zero, not {units(self.quantity)}.')
        if parcel.deactivation_date is not None:
            problems.append(
                f'The parcel bought on {buy.date} was replaced on {parcel.deactivation_date} '
                f'(split, or partly sold), so allocate from the parcel that replaced it.')
        if buy.instrument_id != sell.instrument_id:
            problems.append(
                f'The parcel is {buy.instrument.name} but the sale is {sell.instrument.name}.')
        if buy.date > sell.date:
            problems.append(
                f'The parcel was bought on {buy.date}, after the sale on {sell.date}.')

        others = SellAllocation.objects.filter(is_active=True).exclude(pk=self.pk)
        sold = others.filter(parcel=parcel).aggregate(total=Sum('quantity'))['total'] or 0
        unsold = parcel.parcel_quantity - sold
        if self.quantity > unsold:
            problems.append(
                f'Only {units(unsold)} units of the parcel bought on {buy.date} are unsold, '
                f'not {units(self.quantity)}.')
        allocated = others.filter(sell=sell).aggregate(total=Sum('quantity'))['total'] or 0
        if allocated + self.quantity > sell.quantity:
            problems.append(
                f'The sale on {sell.date} is for {units(sell.quantity)} units and '
                f'{units(allocated)} are already allocated, so {units(self.quantity)} more is '
                f'too many.')
        return problems

    def _is_new_allocation(self) -> bool:
        # A row from an export arrives already handled: its parcel is the sold one.
        return self._state.adding and not self._creation_handled

    def clean(self) -> None:
        super().clean()
        if self._is_new_allocation():
            problems = self.allocation_problems()
            if problems:
                raise ValidationError(problems)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._is_new_allocation():
            problems = self.allocation_problems()
            if problems:
                raise ValueError(' '.join(problems))
        if self.is_active:
            self.description = f'{self.sell.date} {self.sell.instrument.name} | {self.quantity}'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)



class ShareSplit(BaseModel):
    MODEL_DESCRIPTION = 'Events which transform parcels into new parcels with different cost base and quantity.'
    instrument = models.ForeignKey(Instrument, related_name='share_split', on_delete=models.PROTECT,
        help_text='The instrument that split. Enter its name as listed in the Instrument '
                  'table.')
    quantity_before = models.DecimalField(max_digits=16, decimal_places=4, validators=[validate_positive],
        help_text='Units before the split, in the ratio. For a 1-for-4 split, 1.')
    quantity_after = models.DecimalField(max_digits=16, decimal_places=4, validators=[validate_positive],
        help_text='Units after the split, in the ratio. For a 1-for-4 split, 4.')
    date = models.DateField(
        help_text='The ex-date. Trades on it are already in post-split units, so only parcels '
                  'bought before it are split.')
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path,
        help_text='Path to a supporting document. The file is copied into the app.')
    affected_parcels = models.ManyToManyField(Parcel, editable=False,
        help_text='The parcels the split was applied to. Set by the app; do not edit.')
    _creation_handled = models.BooleanField(default=False, editable=False,
        help_text='Whether the app has already applied this split to parcels. Set by the '
                  'app; do not edit.')

    STRUCTURAL_FIELDS: tuple[str, ...] = ('instrument', 'date', 'quantity_before', 'quantity_after')
    POSITIVE_FIELDS: tuple[str, ...] = ('quantity_before', 'quantity_after')

    calculated_split_multiplier = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Units after divided by units before. Set by the app; do not edit.')
    
    @safe_property
    def split_multiplier(self) -> Decimal:
        multiplier = self.quantity_after / self.quantity_before
        return multiplier.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

    @property
    def ratio(self) -> Decimal:
        """`quantity_after / quantity_before` unrounded, for applying the split to parcels."""
        return Decimal(self.quantity_after) / Decimal(self.quantity_before)

    def chronology_problem(self) -> str | None:
        if not self.instrument_id or not self.date:
            return None
        # A sale on the split's own date is in post-split units, so it needs the split applied
        # first, just as a later one does. Only parcels bought before the split are split.
        sold_later = SellAllocation.objects.filter(
            account_id=self.account_id, is_active=True,
            sell__instrument_id=self.instrument_id, sell__date__gte=self.date,
            parcel__buy__date__lt=self.date,
        ).select_related('sell').order_by('sell__date').first()
        if sold_later is None:
            return None
        when = 'on the same day' if sold_later.sell.date == self.date else 'dated before it'
        return (
            f'The sale of {self.instrument.name} on {sold_later.sell.date} has already been '
            f'allocated, and this split is {when}, so that sale was worked out in pre-split '
            f'units. Delete that sale, enter the split, then enter the sale again.')

    def parcels_created(self) -> list['Parcel']:
        """The parcels this split created, found from the parcel tree rather than the links.

        An export does not carry `affected_parcels`, so a restore rebuilds them from this.
        Applying the split replaced each parcel with a single child dated on the split, holding
        its units times the ratio. A sale the same day also replaces a parcel, with two children.
        """
        quantity_field = Parcel._meta.get_field('parcel_quantity')
        multiplier_field = Parcel._meta.get_field('cumulative_split_multiplier')
        replaced = Parcel.objects.filter(
            account_id=self.account_id, buy__instrument_id=self.instrument_id,
            buy__date__lt=self.date, deactivation_date=self.date,
        ).prefetch_related('children')

        created: list[Parcel] = []
        for parent in replaced:
            children = [child for child in parent.children.all() if child.activation_date == self.date]
            if len(children) != 1:
                continue
            child = children[0]
            if (child.parcel_quantity == convert_to_decimal_field(parent.parcel_quantity * self.ratio, quantity_field)
                    and child.cumulative_split_multiplier == convert_to_decimal_field(
                        parent.cumulative_split_multiplier * self.ratio, multiplier_field)):
                created.append(child)
        return created

    def deletion_blocker(self) -> str | None:
        """Why this split cannot be deleted, or None if it can.

        Deleting reverses the split on the parcels it created. Once one of those has been
        sold or split again, its units are in post-split terms elsewhere too, and reversing
        only part of the history would leave the holding inconsistent.
        """
        if self.affected_parcels.filter(deactivation_date__isnull=False).exists():
            return (
                f'The split of {self.instrument.name} on {self.date} cannot be deleted: parcels '
                f'it created have since been sold or split again. Delete those sales or later '
                f'splits first.')
        return None

    calculated_affected_parcels = models.TextField(null=True, blank=True, editable=False,
        help_text='The parcels the split was applied to, as text. Set by the app; do not '
                  'edit.')
    
    @safe_property
    def affected_parcel_list(self) -> str:
        parcels = self.affected_parcels.select_related()
        parcel_list_str = ''
        for parcel in parcels:
            parcel_list_str += f'{parcel}\n'
        return parcel_list_str

    def __str__(self) -> str:
        return f'{self.pk} | {self.date} | Split of {self.instrument.name} | Multiplier = {self.split_multiplier}'


class CostBaseAdjustment(BaseModel):
    MODEL_DESCRIPTION = 'Cost base adjustments applied to instruments, i.e. AMIT cost base adjustments.'
    cost_base_increase = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        help_text='Change to the total cost base for the year, from the annual tax '
                  'statement. Negative for a decrease.')
    instrument = models.ForeignKey(Instrument, related_name='cost_base_adjustment', on_delete=models.PROTECT,
        help_text='The instrument adjusted. Enter its name as listed in the Instrument '
                  'table.')
    financial_year_end_date = models.DateField(help_text='The last day of the financial year the statement covers.')
    exchange_rate = models.ForeignKey(ExchangeRate, related_name='cost_base_adjustment', on_delete=models.PROTECT, blank=True, null=True,
        help_text='Rate to the portfolio currency, for an adjustment in another currency. '
                  'Looked up by the app.')
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path,
        help_text='Path to the annual tax statement. The file is copied into the app.')
    _creation_handled = models.BooleanField(default=False, editable=False,
        help_text='Whether the app has already spread this over parcels. Set by the app; do '
                  'not edit.')

    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
        help_text='Fiscal year the adjustment is for. Set by the app; do not edit.')
    
    @safe_property
    def fiscal_year(self) -> 'FiscalYear':
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.financial_year_end_date)
        return fiscal_year
    
    @property
    def date(self) -> Date:
        # Alias as it is used in the user_directory_path
        return self.financial_year_end_date
    
    calculated_cost_base_increase_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False,
        help_text='The adjustment in the portfolio currency. Set by the app; do not edit.')
    
    @safe_property
    def cost_base_increase_converted(self) -> Money:
        cost_base_increase_converted =  self.cost_base_increase
        if self.exchange_rate:
            cost_base_increase_converted = self.exchange_rate.apply(cost_base_increase_converted)
        return cost_base_increase_converted
    
    allocation_method = models.CharField(
        max_length=8,
        choices=AllocationMethod.choices,
        default=AllocationMethod.QTY_HELD,
        help_text='How the adjustment is spread across parcels. QTY_HELD weights by units '
                  'and days held.',
    )

    #: Spread across parcels once, when it is entered, so none of these can change after.
    STRUCTURAL_FIELDS: tuple[str, ...] = (
        'instrument', 'financial_year_end_date', 'cost_base_increase',
        'cost_base_increase_currency', 'allocation_method')

    @classmethod
    def spread_on_or_after(cls, trade: 'Trade', day: Date,
                           allocated: bool = False) -> 'CostBaseAdjustment | None':
        """The earliest adjustment of `trade`'s instrument already spread by days held,
        for a year ending on or after `day`, or None.

        With `allocated`, only one that reached a parcel: an adjustment for a year nothing
        was held changes with a new buy, but not with a new sale.
        """
        adjustments = cls.objects.filter(
            account_id=trade.account_id, instrument_id=trade.instrument_id,
            allocation_method=AllocationMethod.QTY_HELD, _creation_handled=True,
            financial_year_end_date__gte=day,
        )
        if allocated:
            adjustments = adjustments.filter(cost_base_adjustment_allocation__isnull=False)
        return adjustments.order_by('financial_year_end_date').first()

    @classmethod
    def with_unconverted_allocations(cls, account: 'Account') -> 'QuerySet[CostBaseAdjustment]':
        """Adjustments allocated to parcels in a currency other than the account's.

        Left by a bug fixed in 0.3.0: a foreign-currency adjustment was allocated before it
        had an exchange rate, and `Parcel.total_adjustments` then counts the foreign amount
        as the account's currency. That is a wrong cost base, not just a wrong stored copy.
        It is not repaired automatically, because re-allocating spreads the adjustment over
        the parcels again; deleting it and entering it again does that deliberately.
        """
        stale = CostBaseAdjustmentAllocation.objects.filter(
            account=account, deactivation_date__isnull=True,
        ).exclude(
            cost_base_increase_currency=str(account.currency),
        ).exclude(cost_base_increase=Decimal('0'))
        return cls.objects.filter(id__in=stale.values('cost_base_adjustment_id'))

    def get_description(self) -> str:
        return f'{self.pk} | {self.financial_year_end_date} | Adjustment of {self.instrument.name} | Cost base increase = {self.cost_base_increase}'
    
    def save(self, *args: Any, **kwargs: Any) -> None:
        self.description = self.get_description()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return cast(str, self.description)

                
class CostBaseAdjustmentAllocation(BaseModel):
    MODEL_DESCRIPTION = 'Allocations of cost base adjustments to specific parcels.'

    cost_base_increase = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        help_text='The part of the adjustment given to this parcel. Set by the app; do not '
                  'edit.')
    parcel = models.ForeignKey(Parcel, related_name='cost_base_adjustment_allocation', on_delete=models.PROTECT,
        help_text='The parcel the adjustment part was given to.')
    cost_base_adjustment = models.ForeignKey(CostBaseAdjustment, related_name='cost_base_adjustment_allocation', on_delete=models.CASCADE,
        help_text='The adjustment this part came from.')
    activation_date = models.DateField(null=True, editable=False,
        help_text='Date this allocation took effect. Set by the app; do not edit.')   # not required
    deactivation_date = models.DateField(null=True, editable=False,
        help_text='Date this allocation stopped applying. Blank while it does. Set by the '
                  'app; do not edit.')

    def bifurcate(self, target_parcel: Parcel, remainder_parcel: Parcel,
                  date: Date) -> tuple['CostBaseAdjustmentAllocation', 'CostBaseAdjustmentAllocation']:

        assert self.is_active, "Parcel is inactive"

        target_parcel_qty = target_parcel.parcel_quantity
        remainder_parcel_qty = remainder_parcel.parcel_quantity
        
        target_fraction = target_parcel_qty / (target_parcel_qty + remainder_parcel_qty)

        new_parcel_message = f'This CostBaseAdjustmentAllocation was created by splitting {self.pk} into two separate allocations.'

        original_amount = self.cost_base_increase

        # Rounded to the precision the column actually stores, so the two halves are
        # computed at the precision they will be saved at and still sum to the original.
        # SQLite keeps whatever it is given, but a numeric(19,4) column would round on the
        # way in, and the halves would then no longer reconcile.
        amount_field = self._meta.get_field('cost_base_increase')
        target_amount = Money(
            convert_to_decimal_field(original_amount.amount * target_fraction, amount_field),
            original_amount.currency,
        )

        with transaction.atomic():
            # Create target allocation
            allocation_target = copy.copy(self) # create a shallow copy
            allocation_target.pk = None
            allocation_target.activation_date = date
            allocation_target.parcel = target_parcel
            allocation_target.cost_base_increase = target_amount
            allocation_target.save()
            allocation_target.log_event(new_parcel_message)
            # Create remainder parcel. Its share is what is left rather than the
            # complementary fraction: the field stores four decimal places, so halving an
            # odd amount and rounding both halves loses a hundredth of a cent every time,
            # and a parcel split repeatedly would bleed cost base with nothing to show why.
            allocation_remainder = copy.copy(self) # create a shallow copy
            allocation_remainder.pk = None
            allocation_remainder.activation_date = date
            allocation_remainder.parcel = remainder_parcel
            allocation_remainder.cost_base_increase = original_amount - target_amount
            allocation_remainder.save()
            allocation_remainder.log_event(new_parcel_message)
            # Update old parcel
            self.log_event(f'This allocation was split into {allocation_target.pk} and {allocation_remainder.pk}, then marked as INACTIVE')
            self.deactivation_date = date
            self.save()
        return allocation_target, allocation_remainder

    def move_to(self, parcel: Parcel, date: Date) -> 'CostBaseAdjustmentAllocation':
        """Carry this allocation whole to `parcel`, which replaced its own on `date`.

        For a share split, where one parcel becomes one other. Returns the new allocation.
        """
        with transaction.atomic():
            moved = copy.copy(self) # create a shallow copy
            moved.pk = None
            moved.activation_date = date
            moved.parcel = parcel
            moved.save()
            moved.log_event(f'This CostBaseAdjustmentAllocation was moved from {self.pk} when parcel {self.parcel_id} was replaced by {parcel.pk}.')
            self.log_event(f'This allocation was moved to {moved.pk}, then marked as INACTIVE')
            self.deactivation_date = date
            self.save()
        return moved


    def save(self, *args: Any, **kwargs: Any) -> None:
        self.is_active = self.deactivation_date is None
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.pk} | {self.cost_base_adjustment.financial_year_end_date} | Adjustment of {self.cost_base_adjustment.instrument.name} | Cost base increase = {self.cost_base_increase} | applied to {self.parcel.id}'
    

class Income(BaseModel):
    MODEL_DESCRIPTION = 'Base class for Income, eg Austrlalian Dividends and Distributions.'
    
    class Meta:
        abstract = True

    description = models.CharField(max_length=255, null=True, blank=True, editable=False,
        help_text='Summary of the payment. Set by the app; do not edit.') # Setting this automatically
    instrument = models.ForeignKey(Instrument, related_name='%(class)s', on_delete=models.PROTECT,
        help_text='The instrument that paid. Enter its name as listed in the Instrument '
                  'table.')

    date = models.DateField(help_text='Date the payment was received.')
    quantity = models.DecimalField(max_digits=16, decimal_places=4, validators=[MinValueValidator(0)],
        help_text='Units you were paid on. Normally the units held the day before the '
                  'ex-date.')
    exchange_rate = models.ForeignKey(ExchangeRate, related_name='%(class)s', on_delete=models.PROTECT, blank=True, null=True,
        help_text='Rate to the portfolio currency on the payment date. Looked up for '
                  'foreign currency payments.')

    file = models.FileField(null=True, blank=True, upload_to=user_directory_path,
        help_text='Path to the payment advice or statement. The file is copied into the '
                  'app.')

    calculated_fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
        help_text='Fiscal year the payment date falls in. Set by the app; do not edit.')
    
    @safe_property
    def fiscal_year(self) -> 'FiscalYear':
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.date)
        return fiscal_year

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.is_active:
            # TODO include total income somehow
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name}' # | {self.quantity} unit @ {self.unit_price_converted} / unit'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.pk} | {self.description}'
    

class Dividend(Income):
    MODEL_DESCRIPTION = 'Dividends, including local dividends and foreign dividends.'

    dividend_type = models.CharField(
        max_length=7, choices=DividendType.choices, default=DividendType.LOCAL,
        help_text='LOCAL for an Australian company, FOREIGN for an overseas one.')

    unfranked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Unfranked part of the dividend, per share.')
    franked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Franked part of the dividend, per share. Franking credits are worked out '
                  'from it.')

    local_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Tax withheld in the country of the payer, for the whole payment.')
    foreign_tax_credit = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Foreign tax paid that can be claimed as a credit, for the whole payment.')
    lic_capital_gain = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Capital gain component of a listed investment company dividend, for the '
                  'whole payment.')

    corporate_tax_rate_percentage = models.DecimalField(
        max_digits=5,  # Total digits, including decimal places
        decimal_places=2,  # Number of digits after the decimal
        default=Decimal('30.0'),
        validators=[validate_company_tax_rate],
        help_text="Enter a percentage value (e.g., 25.00 for 25%)"
    )

    def save(self, *args: Any, **kwargs: Any) -> None:
        # The validator runs only in a form. An import saves directly, and would otherwise
        # fail on dividing by zero with nothing to say which row.
        try:
            validate_company_tax_rate(self.corporate_tax_rate_percentage)
        except ValidationError as error:
            raise ValueError(f'{error.messages[0]} ({self.instrument.name} on {self.date})') from error
        super().save(*args, **kwargs)
    
    @safe_property
    def company_rate(self) -> Decimal:
        return self.corporate_tax_rate_percentage / 100

    calculated_total_unfranked_amount = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Unfranked amount per share times units. Set by the app; do not edit.')
    
    @safe_property
    def total_unfranked_amount(self) -> Money:
        return self.unfranked_amount_per_share * self.quantity

    calculated_total_franked_amount = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Franked amount per share times units. Set by the app; do not edit.')
    
    @safe_property
    def total_franked_amount(self) -> Money:
        return self.franked_amount_per_share * self.quantity
    
    calculated_total_franking_credits = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Franking credits on the franked amount, at the company tax rate. Set by '
                  'the app; do not edit.')
    
    @safe_property
    def total_franking_credits(self) -> Money:
        return self.total_franked_amount * self.company_rate / (1 - self.company_rate)
    
    calculated_total_dividend = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Unfranked plus franked amount for the whole payment. Set by the app; do '
                  'not edit.')
    
    @safe_property
    def total_dividend(self) -> Money:
        # A zero amount may be in any currency, so a zero total takes the dividend's own, which
        # its exchange rate converts from.
        return add_currencies(self.total_unfranked_amount, self.total_franked_amount,
                              default_currency=str(self.unfranked_amount_per_share.currency))

    calculated_total_dividend_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Total dividend in the portfolio currency. Set by the app; do not edit.')
    
    @safe_property
    def total_dividend_converted(self) -> Money:

        total_dividend_converted =  self.total_dividend
        if self.exchange_rate:
            total_dividend_converted = self.exchange_rate.apply(total_dividend_converted)
        return total_dividend_converted


class Distribution(Income):
    MODEL_DESCRIPTION = 'Distributions, such as the income received from ETFs'
    distribution_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Cash distribution per unit, in the currency named beside it.')

    # Optional link to the annual statement that explains what this cash was made up of.
    # A payment carries no tax character of its own -- the attribution does, and it is
    # annual, so it is deliberately not broken out onto this row.
    attribution_statement = models.ForeignKey(
        'AttributionStatement', related_name='distributions',
        null=True, blank=True, on_delete=models.SET_NULL,
        help_text='The annual tax statement that explains what this cash was made up of. '
                  'Optional.')
    total_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'),
        help_text='Tax withheld from the payment, for the whole payment.')

    calculated_total_distribution = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Distribution per unit times units. Set by the app; do not edit.')
    
    @safe_property
    def total_distribution(self) -> Money:
        return self.distribution_amount_per_share * self.quantity
    
    calculated_total_distribution_converted = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False,
        help_text='Total distribution in the portfolio currency. Set by the app; do not '
                  'edit.')
    
    @safe_property
    def total_distribution_converted(self) -> Money:
        total_distribution_converted = self.total_distribution
        if self.exchange_rate:
            total_distribution_converted = self.exchange_rate.apply(total_distribution_converted)
        return total_distribution_converted


class DataExport(BaseModel):
    MODEL_DESCRIPTION = 'Data export events, referencing the associated output file.'

    file = models.FileField(null=True, blank=True, editable=False, upload_to=user_directory_path,
        help_text='The exported Excel file. Set by the app; do not edit.')
    account = models.ForeignKey(Account, on_delete=models.PROTECT,
        help_text='The portfolio exported.')
    include_price_history = models.BooleanField(default=False, help_text='Include price history in the export?')

    def __str__(self) -> str:
        return f'{self.created_at.date().isoformat()} | Data Export - {self.account.description}'


class ResidencyPeriod(BaseModel):
    """A period with one Australian tax residency status.

    Used to apportion the CGT discount for days as a foreign or temporary resident
    (s115-105, s115-115). With no periods, residency is undeclared and the flat discount
    applies, flagged in reports. One Australian period covering every holding gives the
    same result.
    """

    MODEL_DESCRIPTION = 'Periods of Australian tax residency, used to apportion the CGT discount.'


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'start_date'], name='residency_period_keys')
        ]
        ordering = ['start_date']

    status = models.CharField(max_length=9, choices=ResidencyStatus.choices,
        help_text='Tax residency status for the period.')
    start_date = models.DateField(help_text='First day of this period.')
    end_date = models.DateField(
        null=True, blank=True, help_text='Last day of this period. Leave blank if ongoing.')

    i1_election_made = models.BooleanField(
        null=True, blank=True,
        help_text='Where this period begins a departure from Australia: did you choose '
                  'under s104-165(2) to disregard the deemed disposal on ceasing '
                  'residency? If so, the assets you held then stay within the Australian '
                  'CGT net until you sell them.',
    )

    def __str__(self) -> str:
        ending = self.end_date.isoformat() if self.end_date else 'ongoing'
        return f'{self.get_status_display()} | {self.start_date.isoformat()} to {ending}'

    def covers(self, day: Date) -> bool:
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    def validate_intrinsic(self) -> None:
        """Reject an end before the start, or an overlap with another period.

        Runs on every save, including imports, so it only checks what holds whatever order
        rows are loaded in.
        """
        if self.start_date is None:
            return
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'The end date is before the start date.'})

        others = ResidencyPeriod.objects.filter(account=self.account, is_active=True)
        if self.pk:
            others = others.exclude(pk=self.pk)
        for other in others.order_by('start_date'):
            overlap_start = max(self.start_date, other.start_date)
            overlap_end = min(self.end_date or date.max, other.end_date or date.max)
            if overlap_start <= overlap_end:
                raise ValidationError(
                    f'This overlaps an existing period ({other}). Residency history has to '
                    f'describe one status at a time.')

    def clean(self) -> None:
        """`validate_intrinsic`, plus: no gaps, no open-ended period before another, and no
        start after the earliest buy.

        Not run on save, as these can be briefly false mid-import. A saved history with
        holes is reported by `cgt.residency.coverage_problems()`.
        """
        super().clean()
        self.validate_intrinsic()
        if self.start_date is None:
            return

        others = list(
            ResidencyPeriod.objects.filter(account=self.account, is_active=True)
            .exclude(pk=self.pk).order_by('start_date')
        )
        periods = sorted(others + [self], key=lambda period: period.start_date)
        for earlier, later in zip(periods, periods[1:]):
            if earlier.end_date is None:
                raise ValidationError(
                    f'{earlier} is open ended, but another period starts afterwards. Give '
                    f'the earlier period an end date.')
            if (later.start_date - earlier.end_date).days != 1:
                raise ValidationError(
                    f'There is a gap between {earlier} and {later}. Residency history has '
                    f'to be continuous, or a holding bought in the gap has no status.')

        earliest_buy = Buy.objects.filter(
            account=self.account, is_active=True).order_by('date').first()
        if earliest_buy and periods[0].start_date > earliest_buy.date:
            raise ValidationError(
                f'Your residency history starts on {periods[0].start_date.isoformat()}, '
                f'after your earliest purchase on {earliest_buy.date.isoformat()}. Extend '
                f'it back, or the discount on that holding cannot be apportioned.')

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.validate_intrinsic()
        super().save(*args, **kwargs)


class AttributionStatement(BaseModel):
    """An annual tax statement from a managed investment trust.

    Records the capital gains the trust attributes to the member. Annual, like
    CostBaseAdjustment, because attributions cannot be split across quarterly payments.
    """

    MODEL_DESCRIPTION = 'Annual tax statements from managed investment trusts (AMMA statements).'

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'instrument', 'financial_year_end_date'],
                name='attribution_statement_keys',
            )
        ]
        ordering = ['financial_year_end_date', 'id']

    instrument = models.ForeignKey(
        Instrument, related_name='attribution_statement', on_delete=models.PROTECT,
        help_text='The fund the statement is from. Enter its name as listed in the '
                  'Instrument table.')
    financial_year_end_date = models.DateField(help_text='The last day of the financial year the statement covers.')
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path,
        help_text='Path to the annual statement. The file is copied into the app.')

    #: The cost base adjustment read off the same statement, where one was recorded.
    cost_base_adjustment = models.OneToOneField(
        'CostBaseAdjustment', related_name='attribution_statement',
        null=True, blank=True, on_delete=models.SET_NULL,
        help_text='The cost base adjustment entered from this same statement. Optional; '
                  'used to check the two agree.',
    )

    gain_subject_to_mit_withholding = models.BooleanField(
        default=False,
        help_text='Tick only if you were a foreign resident when paid and the fund withheld '
                  'managed investment trust (MIT) withholding tax on the taxable Australian '
                  'property part of its capital gain. That tax is final, so the gain is left '
                  'out of the capital gains schedule (s840-815). Leave unticked otherwise: '
                  'a resident, or a fund that did not withhold, still reports the gain.')

    calculated_fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
        help_text='Fiscal year the statement covers. Set by the app; do not edit.')

    @safe_property
    def fiscal_year(self) -> 'FiscalYear':
        fiscal_year, _created = self.account.fiscal_year_type.classify_date(
            self.financial_year_end_date)
        return fiscal_year

    def __str__(self) -> str:
        return f'{self.financial_year_end_date.isoformat()} | {self.instrument.name} attribution'

    def component_total(self, *components: str) -> Decimal:
        """Sum of the named components, zero where none are present."""
        total = Decimal('0')
        for row in self.components.filter(component__in=components, is_active=True):
            total += row.amount.amount
        return total

    @safe_property
    def discounted_capital_gain(self) -> Decimal:
        """Discounted gains as the trust reports them, already halved. Not grossed up."""
        return self.component_total('DISCOUNTED_TAP', 'DISCOUNTED_NTAP')

    @safe_property
    def other_method_capital_gain(self) -> Decimal:
        """Gains the trust worked out without a discount, so not grossed up."""
        return self.component_total('OTHER_TAP', 'OTHER_NTAP')

    @safe_property
    def total_current_year_capital_gain(self) -> Decimal:
        """The grossed up figure: twice the discounted gains, plus the other method ones."""
        return self.discounted_capital_gain * 2 + self.other_method_capital_gain

    @safe_property
    def reconciles(self) -> bool | None:
        """Whether the grossed-up total is within 2 cents of the stated total.

        None if the statement has no stated total.
        """
        stated = self.components.filter(component='TOTAL_CY_CG', is_active=True).first()
        if stated is None:
            return None
        return abs(self.total_current_year_capital_gain - stated.amount.amount) < Decimal('0.02')

    @safe_property
    def stated_cost_base_movement(self) -> Decimal | None:
        """The cost base movement this statement declares (positive is an increase), or None.

        In order of precedence:
        * AMIT increase less decrease, always netted, as the two can be large and equal.
        * Otherwise, the negated non-attributable or tax-deferred amount.
        """
        def total(*components: str) -> Decimal | None:
            found = self.components.filter(component__in=components, is_active=True)
            return sum((row.amount.amount for row in found), Decimal('0')) if found else None

        increase = total('COSTBASE_INCREASE')
        decrease = total('COSTBASE_DECREASE')
        if increase is not None or decrease is not None:
            return (increase or Decimal('0')) - (decrease or Decimal('0'))

        # Pre-AMIT, and the AMIT statements that state a non-attributable amount instead.
        # Both only ever reduce a cost base, so both are negated.
        for component in ('NON_ATTRIBUTABLE', 'TAX_DEFERRED'):
            amount = total(component)
            if amount is not None:
                return -amount
        return None

    @safe_property
    def cost_base_agrees(self) -> bool | None:
        """Whether the linked cost base adjustment is within 2 cents of the stated movement.

        The two are entered independently so this check means something. None if either is
        missing.
        """
        adjustment = getattr(self, 'cost_base_adjustment', None)
        if adjustment is None:
            return None
        stated = self.stated_cost_base_movement
        if stated is None:
            return None
        recorded = adjustment.cost_base_increase.amount
        return abs(stated - recorded) < Decimal('0.02')


class AttributionComponent(BaseModel):
    """One line from an annual tax statement. A row per line, so new lines need no migration."""

    MODEL_DESCRIPTION = 'Individual components of a managed investment trust annual statement.'


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['statement', 'component'], name='attribution_component_keys')
        ]
        ordering = ['statement', 'component']

    statement = models.ForeignKey(
        AttributionStatement, related_name='components', on_delete=models.CASCADE,
        help_text='The annual statement this line is from.')
    component = models.CharField(
        max_length=26, choices=AttributionComponentType.choices,
        help_text='Which line of the statement this is.')
    amount = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY,
        help_text='The amount shown on that line of the statement.')

    def __str__(self) -> str:
        return f'{self.statement.instrument.name} | {self.get_component_display()} | {self.amount}'


class LodgedSnapshotError(ValueError):
    """A capture would replace a snapshot marked as lodged, which records what was filed."""


class CGTReturnSnapshot(BaseModel):
    """A fiscal year's capital gains figures as they stood at a point in time.

    Gains are recomputed on every report, so a calculation change can alter a lodged year.
    Take one before lodging; CGTBasisChangeReport compares it with a fresh calculation.
    """

    MODEL_DESCRIPTION = 'A record of the capital gains figures for a fiscal year as they stood at a point in time.'


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'fiscal_year', 'taken_at'],
                name='cgt_return_snapshot_keys',
            )
        ]
        ordering = ['fiscal_year', 'taken_at']

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name='cgt_return_snapshot',
        help_text='The fiscal year whose figures were captured.')
    taken_at = models.DateField(default=date.today, help_text='The day these figures were captured.')
    basis = models.CharField(
        max_length=16, choices=CGTBasis.choices, default=CGTBasis.LEGACY,
        help_text='Whether the figures used declared residency or an assumption.')
    engine_version = models.CharField(max_length=32, blank=True, help_text='Application version that produced the figures.')

    is_lodged = models.BooleanField(default=False, help_text='Were these the figures actually lodged?')
    lodged_at = models.DateField(null=True, blank=True,
        help_text='Date the return was lodged. Blank if not lodged.')

    def __str__(self) -> str:
        lodged = ' (lodged)' if self.is_lodged else ''
        return f'{self.fiscal_year.name} snapshot {self.taken_at.isoformat()}{lodged}'

    #: The fields captured per row.
    CAPTURED_FIELDS: list[str] = [
        'sell_allocation_id', 'sell_date', 'instrument', 'quantity_sold',
        'days_held', 'proceeds', 'cost_base', 'capital_gain',
    ]

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The captured figures, as a list of plain dicts."""
        return [row.as_dict() for row in self.captured_rows.all()]

    @property
    def totals(self) -> dict[str, Any]:
        if not self.pk:
            return {}
        captured = list(self.captured_rows.all())
        total = sum(
            (row.capital_gain.amount for row in captured if row.capital_gain is not None),
            Decimal('0'),
        )
        return {
            'row_count': len(captured),
            'total_capital_gain': str(total),
        }

    @classmethod
    def capture(cls, account: 'Account', fiscal_year: 'FiscalYear', taken_at: Date | None = None,
                basis: str = 'LEGACY', is_lodged: bool = False) -> 'CGTReturnSnapshot':
        """Snapshot the realised capital gains for one fiscal year.

        A second capture on the same day replaces that day's snapshot, unless it is marked as
        lodged: that raises LodgedSnapshotError, since those are the figures that were filed.
        """
        from share_dinkum_app.reports import RealisedCapitalGainReport
        from share_dinkum_app import version as version_module

        taken_at = taken_at or date.today()
        if cls.objects.filter(account=account, fiscal_year=fiscal_year, taken_at=taken_at,
                              is_lodged=True).exists():
            raise LodgedSnapshotError(
                f'The {fiscal_year.name} snapshot taken on {taken_at} is marked as lodged, so '
                f'it was kept rather than replaced. Untick "is lodged" on it first to replace '
                f'it.')

        df = RealisedCapitalGainReport(account=account).generate()
        if not df.empty:
            df = df[df['fiscal_year'] == fiscal_year.name]

        with transaction.atomic():
            snapshot, _created = cls.objects.update_or_create(
                account=account,
                fiscal_year=fiscal_year,
                taken_at=taken_at,
                defaults={
                    'basis': basis,
                    'engine_version': getattr(version_module, '__version__', ''),
                    'is_lodged': is_lodged,
                },
            )
            snapshot.captured_rows.all().delete()

            CGTReturnSnapshotRow.objects.bulk_create([
                CGTReturnSnapshotRow(
                    account=account,
                    snapshot=snapshot,
                    sell_allocation_id=row['sell_allocation_id'],
                    sell_date=row['sell_date'],
                    instrument=row['instrument'],
                    quantity_sold=row['quantity_sold'],
                    days_held=row['days_held'],
                    proceeds=row['proceeds'],
                    cost_base=row['cost_base'],
                    capital_gain=row['capital_gain'],
                )
                for _, row in df.iterrows()
            ])

        return snapshot
    


class CGTReturnSnapshotRow(BaseModel):
    """One sell allocation, as it stood when a snapshot was taken.

    `sell_allocation_id` is a plain UUID, not a foreign key, so the row outlives the
    allocation if a later sale replaces it.
    """

    MODEL_DESCRIPTION = 'One disposal within a capital gains snapshot, as it stood when taken.'

    class Meta:
        ordering = ['sell_date', 'id']

    snapshot = models.ForeignKey(
        CGTReturnSnapshot, on_delete=models.CASCADE, related_name='captured_rows',
        help_text='The snapshot this disposal belongs to.')

    sell_allocation_id = models.UUIDField(
        null=True, blank=True,
        help_text='The allocation these figures came from. Deliberately not a foreign key: '
                  'the allocation may since have been replaced, which is exactly what a '
                  'snapshot is for.')
    sell_date = models.DateField(null=True, blank=True,
        help_text='Date of the sale.')
    instrument = models.CharField(max_length=255, blank=True,
        help_text='Name of the instrument sold.')
    quantity_sold = models.DecimalField(
        max_digits=16, decimal_places=4, null=True, blank=True,
        help_text='Units sold in this disposal.')
    days_held = models.IntegerField(null=True, blank=True,
        help_text='Days from the buy to the sale.')

    proceeds = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True,
        help_text='Proceeds of this disposal, in the portfolio currency.')
    cost_base = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True,
        help_text='Cost base of this disposal, in the portfolio currency.')
    capital_gain = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True,
        help_text='Gain or loss on this disposal, before any CGT discount.')

    def __str__(self) -> str:
        return f'{self.instrument} | {self.sell_date} | {self.capital_gain}'

    def as_dict(self) -> dict[str, Any]:
        """The captured fields as a dict of Decimal, Money and date values."""
        return {
            'sell_allocation_id': self.sell_allocation_id,
            'sell_date': self.sell_date,
            'instrument': self.instrument,
            'quantity_sold': self.quantity_sold,
            'days_held': self.days_held,
            'proceeds': self.proceeds,
            'cost_base': self.cost_base,
            'capital_gain': self.capital_gain,
        }

class CPIIndex(models.Model):
    """The Consumer Price Index, one row per quarter. Shared, not per account.

    Loaded by `manage.py load_cpi`. Indexation raises if a quarter it needs is missing.
    """

    MODEL_DESCRIPTION = 'Consumer Price Index by quarter, used for cost base indexation.'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quarter_start_date'], name='cpi_index_keys')
        ]
        ordering = ['quarter_start_date']
        verbose_name_plural = 'CPI index'

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False,
        help_text='Unique identifier of the index row.')
    quarter_start_date = models.DateField(
        help_text='First day of the quarter: 1 January, 1 April, 1 July or 1 October.')
    index_number = models.DecimalField(max_digits=12, decimal_places=4,
        help_text='The Consumer Price Index figure for the quarter.')
    source = models.CharField(
        max_length=255, blank=True,
        help_text='Where the figure came from, so a disputed cost base can be traced.')
    created_at = models.DateTimeField(auto_now_add=True,
        help_text='When the row was created.')
    updated_at = models.DateTimeField(auto_now=True,
        help_text='When the row was last changed.')

    def __str__(self) -> str:
        return f'{self.quarter_start_date.isoformat()} | {self.index_number}'

    def clean(self) -> None:
        super().clean()
        if self.quarter_start_date is None:
            return
        if (self.quarter_start_date.month, self.quarter_start_date.day) not in (
                (1, 1), (4, 1), (7, 1), (10, 1)):
            raise ValidationError({
                'quarter_start_date':
                    'A CPI quarter starts on 1 January, 1 April, 1 July or 1 October.'})


class InstrumentValuation(BaseModel):
    """What one unit of an instrument was worth on a day, for a deemed disposal.

    Per unit rather than per parcel, because parcels are replaced when split or partly sold;
    `Parcel.market_value_at()` multiplies it out. `purpose` says which deemed disposal it
    is for (see `ValuationPurpose`).
    """

    MODEL_DESCRIPTION = 'The value of one unit of an instrument on a date, for a deemed disposal.'



    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'instrument', 'valuation_date', 'purpose'],
                name='instrument_valuation_keys')
        ]
        ordering = ['valuation_date']

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name='valuations',
        help_text='The instrument valued. Enter its name as listed in the Instrument table.')
    valuation_date = models.DateField(help_text='The day the value applies to.')
    unit_value = MoneyField(
        max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY,
        help_text='Value of one unit on that day, in the currency named beside it.')
    purpose = models.CharField(
        max_length=12, choices=ValuationPurpose.choices,
        default=ValuationPurpose.CUTOVER_2027,
        help_text='Which deemed disposal the valuation is for.')
    source = models.CharField(
        max_length=13, choices=ValuationSource.choices, default=ValuationSource.USER,
        help_text='Where the figure came from, so a disputed cost base can be traced.')

    def __str__(self) -> str:
        return f'{self.instrument} | {self.valuation_date.isoformat()} | {self.unit_value}'


class CapitalLossCarryForward(BaseModel):
    """A capital loss carried forward from one year to later years.

    Either an opening balance from returns lodged before using this application
    (`is_opening_balance`), or a closed year's loss. Stored rather than recomputed, so a later
    correction does not change losses a lodged return relied on.
    """

    MODEL_DESCRIPTION = 'Capital losses carried forward into a later income year.'

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'fiscal_year'], name='capital_loss_carry_forward_keys')
        ]
        ordering = ['fiscal_year']

    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT,
        help_text='The year the loss was made, not the year it is used in.')
    amount = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        help_text='A positive number. This is a loss; its sign is implied.')
    is_opening_balance = models.BooleanField(
        default=False,
        help_text='Tick where this came from a return lodged before you started using this '
                  'application, rather than from transactions it holds.')

    def __str__(self) -> str:
        origin = 'opening balance' if self.is_opening_balance else 'calculated'
        return f'{self.fiscal_year} | {self.amount} | {origin}'

    def clean(self) -> None:
        super().clean()
        amount = getattr(self.amount, 'amount', None)
        if amount is not None and amount < 0:
            raise ValidationError({
                'amount': 'Record a loss as a positive number. A negative one here would be '
                          'applied as a gain.'})
