# Standard library imports
from datetime import date, timedelta, datetime, UTC
from decimal import Decimal, ROUND_HALF_UP
import copy
import json

# Django imports
from django.db import models, transaction
from django.contrib.auth.models import AbstractUser
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.db.models import Sum, F, Q
from django.db.models.functions import Coalesce
from django.forms.models import model_to_dict

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




class AppUser(AbstractUser):
    MODEL_DESCRIPTION = 'User accounts registered in the application.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    default_account = models.ForeignKey('Account', on_delete=models.SET_NULL, null=True, blank=True)

    @property
    def visible_account(self):
        """The portfolio this user sees: the default they chose, else the first one they created.

        The dashboard and the local auto-login both need this answer, and they have to agree: if
        auto-login picks a user on one basis and the dashboard resolves a portfolio on another, you
        get signed in to an account whose data you cannot see.
        """
        return self.default_account or Account.objects.filter(owner=self).order_by('created_at').first()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields', None)

        # Only modify first_name/last_name if not using update_fields
        if not update_fields:
            self.first_name = self.first_name or ''
            self.last_name = self.last_name or ''

        super().save(*args, **kwargs)

class FiscalYearType(models.Model):
    MODEL_DESCRIPTION = 'A system table used to define configuration for the financial year, such as its start day and month.'
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    description = models.CharField(max_length=40, default='Australian Tax Year', unique=True)
    start_month = models.IntegerField(default=7) # July
    start_day = models.IntegerField(default=1) # 1st (Australia)

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

        return (fiscal_year, created)
    

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

    name = models.CharField(max_length=9, null=True, blank=True, editable=False)

    def __str__(self):
        return self.name or ''
       
    @safe_property
    def start_date(self):
        return date(self.start_year, self.fiscal_year_type.start_month, self.fiscal_year_type.start_day)

    @safe_property
    def end_date(self):
        next_start_year = self.start_year + 1
        next_start = date(next_start_year,
                        self.fiscal_year_type.start_month,
                        self.fiscal_year_type.start_day)
        return next_start - timedelta(days=1)


    def get_name(self):
        if self.fiscal_year_type.start_month == 1:
            return f'{self.start_year}'
        else:
            return f'FY{self.start_year}/{str(self.start_year + 1)[2:]}'
        
    def save(self, *args, **kwargs):
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

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    description = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    owner = models.ForeignKey(AppUser, on_delete=models.PROTECT)
    fiscal_year_type = models.ForeignKey(FiscalYearType, on_delete=models.PROTECT)
    update_price_history = models.BooleanField(default=False)


    #: Decides what discount is available at all: an individual halves a capital gain, a
    #: complying superannuation fund takes a third off, a company gets nothing. Left
    #: undeclared rather than defaulted to an individual, because guessing wrong here is a
    #: 50 to 100 per cent error on every gain the portfolio makes.
    taxpayer_type = models.CharField(
        max_length=11, choices=TaxpayerType.choices, default=TaxpayerType.UNDECLARED,
        help_text='Who owns this portfolio for tax purposes.')

    #: Set once the holder has been through the tax settings, whatever they chose there.
    #: It is what stops the dashboard nagging, so answering the question is enough --
    #: a warning that cannot be dismissed by answering it just teaches people to ignore it.
    tax_settings_reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return f'{self.description} | {self.currency}'

    calculated_portfolio_value_converted = MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, default_currency=DEFAULT_CURRENCY)

    @safe_property
    def portfolio_value_converted(self):
        return Instrument.objects.filter(account=self, is_active=True).aggregate(models.Sum('calculated_value_held_converted'))['calculated_value_held_converted__sum'] or Money(0, self.currency)

    def update_all_price_history(self):
        """
        Update price history for instruments held in this account.

        Instruments with an open position are always refreshed. Instruments that have been fully
        sold continue to refresh until at least one data point exists after their final sell date.
        """
        instruments = Instrument.objects.filter(account=self, is_active=True)

        for instrument in instruments:
            if instrument.quantity_held > 0:
                instrument.update_price_history()
                continue

            last_sell_date = (
                Sell.objects.filter(account=self, instrument=instrument)
                .order_by('-date')
                .values_list('date', flat=True)
                .first()
            )

            if not last_sell_date:
                continue

            has_history_after_sell = InstrumentPriceHistory.objects.filter(
                account=self,
                instrument=instrument,
                date__gte=last_sell_date,
            ).exists()

            if has_history_after_sell:
                continue

            instrument.update_price_history(end_date=date.today())


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

    CALCULATED_FIELDS = frozenset({
        'calculated_portfolio_value_converted',
        'calculated_portfolio_value_converted_currency',
    })

    def save(self, *args, **kwargs):
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
        user = kwargs.pop('user', None)
        super().save(*args, **kwargs)
                
    def __str__(self):
        return f'{self.description}'


class LogEntry(BaseModel):
    MODEL_DESCRIPTION = 'Log entries. Key events are recorded here.'
    event = models.CharField(max_length=255)
    notes = None # Don't want notes on logs
    # Generic Foreign Key fields
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name='logs', editable=False)
    object_id = models.UUIDField()
    content_object = GenericForeignKey('content_type', 'object_id')

    def __str__(self):
        return f'{self.created_at.isoformat(timespec="seconds")} - ***{(str(self.pk))[-4:]} - {self.event}'


class AbstractExchangeRate(models.Model): # Not using BaseModel as doesn't need description, notes, is_active, created_at etc
    MODEL_DESCRIPTION = 'Abstract base class for exchange rates between pairs of currencies'

    class Meta:
        abstract = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    convert_to = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    convert_from = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    exchange_rate_multiplier = models.DecimalField(max_digits=16, decimal_places=6, default=Decimal('1.0'))

    def apply(self, money):
        assert str(money.currency) == str(self.convert_from), (
            f'Invalid exchange rate applied. The convert_from currency {self.convert_from} '
            f'does not match the currency {money.currency}'
        )

        new_amount = money.amount * self.exchange_rate_multiplier

        return Money(new_amount, str(self.convert_to))

    def update_current(self):
        """Update or create the CurrentExchangeRate for this historical rate.

        Only the most recent known rate may drive the "current" rate. A backfilled
        or older row must not clobber a more recent value with a stale figure.
        """

        if hasattr(self, 'date'):
            newer_exists = type(self).objects.filter(
                account=self.account,
                convert_from=self.convert_from,
                convert_to=self.convert_to,
                date__gt=self.date,
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
    

    def __str__(self):

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
    def get_or_create(cls, account, convert_from, convert_to, force_refresh=False):
        """
        Get the current exchange rate. If missing, stale (>1hr), or force_refresh=True,
        update history and refresh latest value.
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
    
    date = models.DateField()
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
                defaults={'exchange_rate_multiplier' : Decimal('1.0')}
            )
            if created:
                field = cls._meta.get_field('exchange_rate_multiplier')
                fetched_rate = yfinanceinterface.get_exchange_rate(
                    convert_from=convert_from,
                    convert_to=convert_to,
                    exchange_date=exchange_date,
                )
                if fetched_rate is not None:
                    obj.exchange_rate_multiplier = convert_to_decimal_field(fetched_rate, field)
                    obj.save(update_fields=['exchange_rate_multiplier'])
                elif convert_from != convert_to:
                    # A failed cross-currency fetch must NOT keep the 1.0 default - that
                    # relabels foreign amounts as base currency (e.g. USD shown as AUD).
                    # Fall back to the most recent known rate for this pair instead.
                    fallback = cls.objects.filter(
                        account=account,
                        convert_from=convert_from,
                        convert_to=convert_to,
                    ).exclude(pk=obj.pk).order_by('-date').first()
                    if fallback is not None:
                        obj.exchange_rate_multiplier = fallback.exchange_rate_multiplier
                        obj.save(update_fields=['exchange_rate_multiplier'])
                        logger.warning(
                            "Could not fetch exchange rate for %s to %s on %s; using most "
                            "recent known rate from %s (%s).",
                            convert_from, convert_to, exchange_date,
                            fallback.date, fallback.exchange_rate_multiplier,
                        )
                    else:
                        logger.error(
                            "Could not fetch exchange rate for %s to %s on %s and no prior "
                            "rate exists; leaving multiplier at 1.0. Figures for this currency "
                            "will be unconverted until a rate is available.",
                            convert_from, convert_to, exchange_date,
                        )

            obj.update_current()
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
            if earliest_buy:
                start_date = earliest_buy.date
            else:
                return  # No buys, so no need to fetch exchange rates
            
        try:

            price_history = yfinanceinterface.get_exchange_rate_history(convert_from=convert_from, convert_to=convert_to, start_date=start_date)

            field = cls._meta.get_field('exchange_rate_multiplier')
            price_history['exchange_rate_multiplier'] = price_history['exchange_rate_multiplier'].apply(
                lambda val: convert_to_decimal_field(val, field)
            )

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

            if not price_history.empty:
                latest_row = price_history.loc[price_history['date'].idxmax()]
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
                    exchange_rate_multiplier=latest_multiplier
                )
                latest.update_current()
                return latest


        except Exception as e:
            logger.error(f'Error getting exchange rate history for {convert_from} to {convert_to}, {e}', exc_info=True)



class Market(BaseModel):
    MODEL_DESCRIPTION = 'Share markets, such as ASX, NASDAQ, LSE, etc'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'code'], name='market_keys')
        ]

    code = models.CharField(max_length=16)

    suffix = models.CharField(max_length=16, null=True, blank=True)

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

    #: How the legal form came to be set. A suggestion is never treated as settled: a
    #: capital gains schedule built on unconfirmed classifications is reported as a draft.

    name = models.CharField(max_length=16)
    description = models.CharField(max_length=255, blank=True)
    currency = CurrencyField(default=DEFAULT_CURRENCY, choices=CURRENCY_CHOICES)
    market = models.ForeignKey(Market, on_delete=models.PROTECT)
    current_unit_price = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True)

    legal_form = models.CharField(
        max_length=13, choices=LegalForm.choices, default=LegalForm.UNKNOWN,
        help_text='Shown on the product disclosure statement or annual tax statement.',
    )
    #: Where the legal form came from, not something to be filled in. It is set for you:
    #: change the legal form yourself and it becomes USER, which is what marks the answer
    #: as confirmed. Nothing else does, and until it happens every schedule built on the
    #: instrument is a draft.
    legal_form_source = models.CharField(
        max_length=9, choices=LegalFormSource.choices, default=LegalFormSource.DEFAULT,
        editable=False,
    )
    #: One of the eight boxes on the schedule, or nothing. Free text here would put
    #: whatever was typed straight onto a tax return as a category, which is the one thing
    #: these labels exist to prevent -- they are the form's vocabulary, not a description.
    cgt_asset_category_override = models.CharField(
        max_length=48, null=True, blank=True,
        choices=CGTAssetCategory.reportable_choices(),
        help_text='Leave empty. Only set this if the category worked out from the legal '
                  'form and the market is wrong for this holding.',
    )
    #: Suppresses the derivation, rather than recording a fact. Named for what it does,
    #: because the previous name -- `is_taxable_australian_property` -- read as a question
    #: about the instrument, and a blank field phrased as a question invites an answer.
    #: Answering "no" is the trap: it is the truthful answer about an ordinary listed share
    #: on its own account, and it also switches off the s104-165(3) deeming that is the main
    #: way such a share becomes taxable Australian property. Leaving it empty is what lets
    #: that question be asked per parcel, which is where it belongs.
    is_taxable_australian_property_override = models.BooleanField(
        null=True, blank=True,
        help_text='Leave empty. Only set this if the instrument is taxable Australian '
                  'property in its own right -- real property, or a non-portfolio interest '
                  'in a land rich entity. Setting it to "no" is not the same as leaving it '
                  'empty: it overrides the departure deeming for every parcel, including '
                  'ones you held when you ceased Australian residency.',
    )

    @safe_property
    def cgt_asset_category(self):
        """Which box on the CGT schedule a gain on this instrument belongs in."""
        from share_dinkum_app.cgt import classification
        return classification.asset_category(self)

    @safe_property
    def is_classified(self):
        """Whether the user has confirmed what this instrument legally is.

        A suggestion is not a confirmation. The application can infer a legal form from a
        ticker or from what a holding has paid, and it is usually right, but a capital gains
        schedule reports where a gain goes on a tax return and that should rest on someone
        having said so rather than on a guess that happened to be good.
        """
        return (self.legal_form != LegalForm.UNKNOWN
                and self.legal_form_source == LegalFormSource.USER)

    def save(self, *args, **kwargs):
        """Record who decided the legal form, so nobody has to maintain that by hand.

        `legal_form_source` is not editable, and until this existed nothing ever set it to
        USER -- so `is_classified` could never be true and the schedule report could never
        stop calling itself a draft. The gate was there with no way through it.

        Setting the legal form yourself is the confirmation. Anything that means it
        differently, which is the suggester, says so by setting the source in the same save.
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

    calculated_quantity_held = models.DecimalField(max_digits=16, decimal_places=4, blank=True, null=True, editable=False)
    

    @safe_property
    def quantity_held(self):
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

    calculated_value_held =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def value_held(self):
        if self.current_unit_price:
            value_held = Money(self.current_unit_price * self.quantity_held, self.currency)
        else:
            if not self.currency:
                raise ValueError(f'Instrument {self} has no currency set.')
            else:
                value_held = Money(0, self.currency)
        return value_held

    calculated_value_held_converted =  MoneyField(max_digits=19, decimal_places=4, null=True, blank=True, editable=False)
    
    @safe_property
    def value_held_converted(self):
        
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
            logger.error(f'No exchange rate available for {self.currency} to {self.account.currency}')
            logger.error(f'Instrument: {self}, Account: {self.account}, Currency: {self.currency}')
            
            raise ValueError(
                f"No exchange rate available for {self.currency} to {self.account.currency}"
            )

        converted_value = current_rate.apply(self.value_held)
        assert isinstance(converted_value, Money), f'Converted value held is not a Money instance: {converted_value}'
        return converted_value


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
            return f'{self.name} - {self.description} [{self.account.description}]'
        else:
            return f'{self.name} - {self.description} (INACTIVE)'

    def update_price_history(self, end_date=None):
        """
        Refresh price history data for this instrument up to the supplied end_date.

        When no end_date is provided the current date is used. The fetch always rewinds a few days
        from the most recent stored price to account for weekends or suspensions.
        """
        end_date = end_date or date.today()

        latest_price_history = (
            InstrumentPriceHistory.objects.filter(instrument=self)
            .order_by('-date')
            .first()
        )

        if latest_price_history:
            start_date = latest_price_history.date - timedelta(days=4)
            if start_date > end_date:
                start_date = end_date
        else:
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
            return

        try:
            price_history = yfinanceinterface.get_instrument_price_history(
                instrument=self,
                start_date=start_date,
                end_date=end_date,
            )
            if price_history.empty:
                logger.warning('No price history returned for %s between %s and %s', self, start_date, end_date)
                return

            decimal_fields = {
                field_name: InstrumentPriceHistory._meta.get_field(field_name)
                for field_name in ['open', 'high', 'low', 'close', 'stock_splits']
            }
            for column, field in decimal_fields.items():
                price_history[column] = price_history[column].apply(
                    lambda val: convert_to_decimal_field(val, field)
                )

            price_history['account'] = self.account
            price_history['id'] = price_history['date'].apply(lambda x: uuid7())

            # Drop rows with null close prices (yfinance sometimes returns NaN for recent dates)
            price_history = price_history.dropna(subset=['close'])
            if price_history.empty:
                logger.warning('No valid close prices for %s between %s and %s', self, start_date, end_date)
                return

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

            price_history_entries = []
            for _, row in price_history.iterrows():
                price_history_entries.append(
                    InstrumentPriceHistory(
                        **row.to_dict()
                    )
                )

            with transaction.atomic():
                InstrumentPriceHistory.objects.bulk_create(price_history_entries, ignore_conflicts=True)

        except Exception as e:
            logger.error(f'Error getting price history for {self} between {start_date} and {end_date}, {e}', exc_info=True)



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
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.date)
        return fiscal_year
    
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
        logger.debug('Calculating unit price converted on %s', self)
        unit_price_converted =  self.unit_price
        if self.exchange_rate:
            unit_price_converted = self.exchange_rate.apply(unit_price_converted)
            logger.debug('Converted unit price is %s', unit_price_converted)
        else:
            logger.debug('No exchange rate available for %s', self)
        return unit_price_converted

    def __str__(self):
        return f'{self.description}'

    def save(self, *args, **kwargs):
        if self.is_active:
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name} | {self.quantity} unit @ {self.unit_price} / unit'
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


class Sell(Trade):
    MODEL_DESCRIPTION = 'Sales of shares.'
    _creation_handled = models.BooleanField(default=False, editable=False)

    
    strategy = models.CharField(
        max_length=7,
        choices=SellStrategy.choices,
        default=SellStrategy.MIN_CGT,
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
    
    def clean(self):
        super().clean()
        # Ensure an exchange rate is provided for cross-currency sells
        if self.instrument.currency != self.account.currency and not self.exchange_rate:
            raise ValidationError(
                "Exchange rate is required when instrument currency differs from account currency."
            )


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
    cumulative_split_multiplier = models.DecimalField(max_digits=16, decimal_places=4, editable=False, default=Decimal('1.0'))
    activation_date = models.DateField(null=True, editable=False)
    deactivation_date = models.DateField(null=True, editable=False)
    sale_date = models.DateField(null=True, editable=False)

    calculated_instrument_name = models.CharField(max_length=16, null=True, blank=True, editable=False)
    
    @safe_property
    def instrument_name(self):
        return self.buy.instrument.name if self.buy and self.buy.instrument else None

    calculated_remaining_quantity = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, editable=False)

    @safe_property
    def remaining_quantity(self):

        if not self.is_active:
            return Decimal('0')
        
        sold_quantity = self.sale_allocation.filter(is_active=True).aggregate(total_allocated=Sum('quantity'))['total_allocated'] or 0
        return self.parcel_quantity - sold_quantity

    calculated_is_sold = models.BooleanField(null=True, blank=True, editable=False)
    
    @safe_property
    def is_sold(self):
        return self.remaining_quantity <= Decimal('0') # Using <= to account for any potential rounding issues


    @safe_property
    def adjusted_buy_price(self):
        adjusted_buy_price = self.buy.unit_price_converted / self.cumulative_split_multiplier
        return adjusted_buy_price

    calculated_adjusted_unit_brokerage = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def adjusted_unit_brokerage(self):

        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        adjusted_unit_brokerage = self.buy.unit_brokerage_converted / self.cumulative_split_multiplier
        return adjusted_unit_brokerage

    
    @safe_property
    def total_adjustments(self):
        total_adjustment = self.cost_base_adjustment_allocation.filter(
            deactivation_date__isnull=True
        ).aggregate(
            total=Sum('cost_base_increase')
        )['total'] or Decimal('0')

        total_adjustment = Money(total_adjustment, self.buy.account.currency)  # TODO assumes all in base currency
        return total_adjustment
    
    calculated_total_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_cost_base(self):

        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        # Already converted
        parcel_quantity = self.parcel_quantity
        total_cost_base = (self.adjusted_buy_price * parcel_quantity)
        total_cost_base += (self.adjusted_unit_brokerage * parcel_quantity)
        total_cost_base = add_currencies(total_cost_base, self.total_adjustments)

        return total_cost_base
    
    calculated_unit_cost_base = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def unit_cost_base(self):
        
        if not self.is_active:
            return Money(Decimal('0'), self.buy.account.currency)

        return self.total_cost_base / self.parcel_quantity

    def market_value_at(self, day, purpose='CUTOVER_2027'):
        """What this parcel was worth on a day, for a deemed disposal.

        Computed from a per-unit valuation rather than stored against the parcel, which is
        what keeps it correct across a later split or partial sale. A parcel is replaced,
        not mutated, by `bifurcate()` and `split_or_consolidate()`, so a stored per-parcel
        value would detach from its parcel the first time either ran, exactly as the cost
        base adjustment allocations needed hand-written code to avoid.

        Returns `(value, source)`, or `(None, None)` where no valuation is available. The
        caller decides what to do about that; nothing here substitutes a guess.
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
        return (unit_value / multiplier) * self.parcel_quantity, source

    def split_or_consolidate(self, multiplier, date):
        assert multiplier > 0
        assert self.is_active

        new_parcel_message = f'This parcel was created by splitting parcel {self.pk} by multiplier {multiplier}'

        with transaction.atomic():
            # Create target parcel
            parcel_target = copy.copy(self) # create a shallow copy
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

        return parcel_target

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

    def __str__(self):
        if self.is_active:
            parcel_desc  = f'{self.description} @ {self.adjusted_buy_price} / unit | Total cost base = {self.total_cost_base} |'
            if self.is_sold:
                parcel_desc  += ' SOLD'
            return parcel_desc 
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
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.sale_date)
        return fiscal_year
    
    calculated_days_held = models.IntegerField(null=True, blank=True, editable=False)
    
    @safe_property
    def days_held(self):
        return (self.sell.date - self.parcel.buy.date).days
    
    calculated_total_capital_gain = MoneyField(max_digits=19, decimal_places=6, null=True, blank=True, editable=False)
    
    @safe_property
    def total_capital_gain(self):
        # Note, a parcel is always fully consumed by a sell allocation due to the bifurcation process, therefore can just use parcel.total_cost_base rather than unit cost base and qty. This avoids rounding issues
        return (self.sell.proceeds * self.quantity / self.sell.quantity) - self.parcel.total_cost_base

    def save(self, *args, **kwargs):
        if self.is_active:
            self.description = f'{self.sell.date} {self.sell.instrument.name} | {self.quantity}'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)



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
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.financial_year_end_date)
        return fiscal_year
    
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
    
    allocation_method = models.CharField(
        max_length=8,
        choices=AllocationMethod.choices,
        default=AllocationMethod.QTY_HELD,
    )

    def get_description(self):
        return f'{self.pk} | {self.financial_year_end_date} | Adjustment of {self.instrument.name} | Cost base increase = {self.cost_base_increase}'
    
    def save(self, *args, **kwargs):
        self.description = self.get_description()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description

                
class CostBaseAdjustmentAllocation(BaseModel):
    MODEL_DESCRIPTION = 'Allocations of cost base adjustments to specific parcels.'

    cost_base_increase = MoneyField(max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY)
    parcel = models.ForeignKey(Parcel, related_name='cost_base_adjustment_allocation', on_delete=models.PROTECT)
    cost_base_adjustment = models.ForeignKey(CostBaseAdjustment, related_name='cost_base_adjustment_allocation', on_delete=models.CASCADE)
    activation_date = models.DateField(null=True, editable=False)   # not required
    deactivation_date = models.DateField(null=True, editable=False)

    def bifurcate(self, target_parcel, remainder_parcel, date):

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
        return
    

    def save(self, *args, **kwargs):
        self.is_active = self.deactivation_date is None
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.pk} | {self.cost_base_adjustment.financial_year_end_date} | Adjustment of {self.cost_base_adjustment.instrument.name} | Cost base increase = {self.cost_base_increase} | applied to {self.parcel.id}'
    

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
        fiscal_year, _ = self.account.fiscal_year_type.classify_date(input_date=self.date)
        return fiscal_year

    def save(self, *args, **kwargs):
        if self.is_active:
            # TODO include total income somehow
            self.description = f'{self.date} | {self.__class__.__name__} | {self.instrument.name}' # | {self.quantity} unit @ {self.unit_price_converted} / unit'
        else:
            self.description = 'INACTIVE'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.pk} | {self.description}'
    

class Dividend(Income):
    MODEL_DESCRIPTION = 'Dividends, including local dividends and foreign dividends.'

    dividend_type = models.CharField(
        max_length=7, choices=DividendType.choices, default=DividendType.LOCAL)

    unfranked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))
    franked_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))

    local_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))
    foreign_tax_credit = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))
    lic_capital_gain = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))

    corporate_tax_rate_percentage = models.DecimalField(
        max_digits=5,  # Total digits, including decimal places
        decimal_places=2,  # Number of digits after the decimal
        default=Decimal('30.0'),
        help_text="Enter a percentage value (e.g., 25.00 for 25%)"
    )
    
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
    distribution_amount_per_share = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))

    # Optional link to the annual statement that explains what this cash was made up of.
    # A payment carries no tax character of its own -- the attribution does, and it is
    # annual, so it is deliberately not broken out onto this row.
    attribution_statement = models.ForeignKey(
        'AttributionStatement', related_name='distributions',
        null=True, blank=True, on_delete=models.SET_NULL)
    total_withholding_tax = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY, default=Decimal('0'))

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
    include_price_history = models.BooleanField(default=False, help_text='Include price history in the export?')

    def __str__(self):
        return f'{self.created_at.date().isoformat()} | Data Export - {self.account.description}'


class ResidencyPeriod(BaseModel):
    """A period over which the account holder had one Australian tax residency status.

    The CGT discount is not a flat 50% for everyone. s115-105 and s115-115 reduce it in
    proportion to the days the holder was a foreign or temporary resident, so the
    application cannot work out a discount at all without knowing who was where and when.

    Nothing is assumed. An account with no residency periods is treated as undeclared and
    keeps the flat 50% the application has always applied, with every report saying so.
    Declaring a single period of Australian residency covering the whole holding produces
    exactly 50% again -- resident days equal total days -- so for a taxpayer who has always
    lived in Australia this changes no figure at all. It is only ever the periods abroad
    that move a number.
    """

    MODEL_DESCRIPTION = 'Periods of Australian tax residency, used to apportion the CGT discount.'


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'start_date'], name='residency_period_keys')
        ]
        ordering = ['start_date']

    status = models.CharField(max_length=9, choices=ResidencyStatus.choices)
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

    def __str__(self):
        ending = self.end_date.isoformat() if self.end_date else 'ongoing'
        return f'{self.get_status_display()} | {self.start_date.isoformat()} to {ending}'

    def covers(self, day):
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    def validate_intrinsic(self):
        """Checks that do not depend on what else has been loaded yet.

        These run on every save, including an Excel import, which does not go through a
        form and so never calls `clean()`. They are limited to what is true regardless of
        the order rows arrive in: an import that happens to load a later period first would
        otherwise be rejected for a gap that the next row fills.
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

    def clean(self):
        """Everything `validate_intrinsic` checks, plus that the history hangs together.

        A gap is not a neutral absence: a parcel bought inside one has no residency status,
        and any status invented for it would silently decide a tax outcome. The same goes
        for a history that starts after the earliest purchase.

        These two live here rather than in `save()` because they are properties of the
        whole set rather than of one row, so they can be transiently false part way through
        a bulk load. Where a saved history does end up incomplete,
        `cgt.residency.coverage_problems()` reports it and the affected gains are marked
        rather than discounted on a guess.
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

    def save(self, *args, **kwargs):
        self.validate_intrinsic()
        super().save(*args, **kwargs)


class AttributionStatement(BaseModel):
    """An annual tax statement from a managed investment trust.

    A trust does not only pay cash: it *attributes* its own income to members, including
    capital gains it made selling assets the member never held. Those gains are the
    member's for tax purposes, and for a portfolio of ETFs they are frequently larger than
    anything the member realised themselves. The application had no way to record them.

    Modelled on the statement rather than on the payment, because that is how the
    information arrives. A trust distributes quarterly but reports once a year, and there
    is no defensible way to split an annual attribution across four cash payments -- so
    this sits alongside CostBaseAdjustment, which is annual for the same reason and is
    usually read off the very same document.
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
        Instrument, related_name='attribution_statement', on_delete=models.PROTECT)
    financial_year_end_date = models.DateField()
    file = models.FileField(null=True, blank=True, upload_to=user_directory_path)

    #: The cost base adjustment read off the same statement, where one was recorded.
    cost_base_adjustment = models.OneToOneField(
        'CostBaseAdjustment', related_name='attribution_statement',
        null=True, blank=True, on_delete=models.SET_NULL,
    )

    calculated_fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.SET_NULL, null=True, blank=True, editable=False)

    @safe_property
    def fiscal_year(self):
        fiscal_year, _created = self.account.fiscal_year_type.classify_date(
            self.financial_year_end_date)
        return fiscal_year

    def __str__(self):
        return f'{self.financial_year_end_date.isoformat()} | {self.instrument.name} attribution'

    def component_total(self, *components):
        """Sum of the named components, zero where none are present."""
        total = Decimal('0')
        for row in self.components.filter(component__in=components, is_active=True):
            total += row.amount.amount
        return total

    @safe_property
    def discounted_capital_gain(self):
        """The trust's discounted capital gains attributed to this member.

        Halved already, as the trust reports them. The member grosses them up, applies
        their own capital losses, then applies their own discount percentage -- which is
        why the schedule wants the grossed up figure rather than this one.
        """
        return self.component_total('DISCOUNTED_TAP', 'DISCOUNTED_NTAP')

    @safe_property
    def other_method_capital_gain(self):
        """Gains the trust worked out without a discount, so not grossed up."""
        return self.component_total('OTHER_TAP', 'OTHER_NTAP')

    @safe_property
    def total_current_year_capital_gain(self):
        """The grossed up figure: twice the discounted gains, plus the other method ones."""
        return self.discounted_capital_gain * 2 + self.other_method_capital_gain

    @safe_property
    def reconciles(self):
        """Whether the components agree with the total the statement itself states.

        Twice the discounted gains plus the other method gains must equal the stated total.
        Where it does not, the statement was misread or the layout was not understood, and
        the figures should not be relied on. Returns None where the statement does not
        state a total to check against.
        """
        stated = self.components.filter(component='TOTAL_CY_CG', is_active=True).first()
        if stated is None:
            return None
        return abs(self.total_current_year_capital_gain - stated.amount.amount) < Decimal('0.02')

    @safe_property
    def stated_cost_base_movement(self):
        """The cost base movement this statement declares, signed, or None.

        Positive increases the cost base. Three shapes of statement say this three ways, and
        the order below is the order they take precedence in, because a statement that
        states an AMIT cost base net amount states the governing figure even where other
        non-assessable lines also appear.

        **The AMIT pair must be netted, never read one leg at a time.** A statement can
        declare an excess and a shortfall that are both large and exactly equal -- 1,958.03
        each way, netting to nil, is a real example from this portfolio -- so a check against
        the shortfall alone would report a 1,958.03 discrepancy where the correct answer is
        zero.
        """
        def total(*components):
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
    def cost_base_agrees(self):
        """Whether the linked cost base adjustment matches what this statement states.

        The two are entered separately and deliberately stay that way: the adjustment moves
        parcel cost bases and the statement records what the issuer said, and a check is only
        worth having while both sides are read independently. Deriving one from the other
        would make them agree by construction and detect nothing.

        Returns None where there is nothing to compare -- no linked adjustment, or no cost
        base line transcribed -- because an absent check is not a passing one.
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
    """One line from an annual tax statement.

    Long and narrow rather than a column per line: statements differ between issuers and
    gain new categories over time, so a new component is a new row rather than a migration.
    """

    MODEL_DESCRIPTION = 'Individual components of a managed investment trust annual statement.'


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['statement', 'component'], name='attribution_component_keys')
        ]
        ordering = ['statement', 'component']

    statement = models.ForeignKey(
        AttributionStatement, related_name='components', on_delete=models.CASCADE)
    component = models.CharField(
        max_length=26, choices=AttributionComponentType.choices)
    amount = MoneyField(max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY)

    def __str__(self):
        return f'{self.statement.instrument.name} | {self.get_component_display()} | {self.amount}'


class CGTReturnSnapshot(BaseModel):
    """What the capital gains figures were for a fiscal year, at a point in time.

    Capital gains figures are derived, never stored, so improving a calculation silently
    changes what the app reports for years the user may already have lodged. A snapshot
    records the figures as they stood, so a later change can be detected and explained
    rather than quietly replacing a number someone filed a return on.

    Take one before lodging. CGTBasisChangeReport compares it against a fresh calculation.
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

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name='cgt_return_snapshot')
    taken_at = models.DateField(default=date.today, help_text='The day these figures were captured.')
    basis = models.CharField(
        max_length=16, choices=CGTBasis.choices, default=CGTBasis.LEGACY)
    engine_version = models.CharField(max_length=32, blank=True, help_text='Application version that produced the figures.')

    is_lodged = models.BooleanField(default=False, help_text='Were these the figures actually lodged?')
    lodged_at = models.DateField(null=True, blank=True)

    def __str__(self):
        lodged = ' (lodged)' if self.is_lodged else ''
        return f'{self.fiscal_year.name} snapshot {self.taken_at.isoformat()}{lodged}'

    #: Only the fields worth comparing are captured. Identifiers locate a row; the rest are
    #: the figures that can move.
    CAPTURED_FIELDS = [
        'sell_allocation_id', 'sell_date', 'instrument', 'quantity_sold',
        'days_held', 'proceeds', 'cost_base', 'capital_gain',
    ]

    @property
    def rows(self):
        """The captured figures, as a list of plain dicts."""
        return [row.as_dict() for row in self.captured_rows.all()]

    @property
    def totals(self):
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
    def capture(cls, account, fiscal_year, taken_at=None, basis='LEGACY', is_lodged=False):
        """Record the realised capital gain figures for one fiscal year.

        Re-capturing on the same day replaces that day's snapshot rather than accumulating
        near-duplicates; capturing on a later day adds to the history.
        """
        from share_dinkum_app.reports import RealisedCapitalGainReport
        from share_dinkum_app import version as version_module

        df = RealisedCapitalGainReport(account=account).generate()
        if not df.empty:
            df = df[df['fiscal_year'] == fiscal_year.name]

        with transaction.atomic():
            snapshot, _created = cls.objects.update_or_create(
                account=account,
                fiscal_year=fiscal_year,
                taken_at=taken_at or date.today(),
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
    """One disposal, as it stood when a snapshot was taken.

    These were a single JSON blob on the snapshot, held as text so it would survive the
    Excel export and import round trip through one cell. A cell holds 32,767 characters,
    which worked out at about three hundred rows -- and rows are *sell allocations*, not
    sales, so one sale spanning twelve parcels is twelve of them. Anyone trading actively
    reached the cap, and `capture()` refused rather than truncating, so the feature simply
    stopped working for them.

    As ordinary rows there is no cap, the figures are queryable, and a snapshot exports as
    its own sheet instead of an unreadable wall of JSON in one cell.

    **`sell_allocation_id` is a plain UUID, not a foreign key**, and that is the whole point
    of the model. A snapshot exists to record what the figures were *before* something
    changed, and what changed is often the allocation itself: a later sale bifurcates a
    parcel and its allocations are replaced. A foreign key would either block that with
    PROTECT, or destroy the evidence with CASCADE. The identifier is kept as a value so the
    record outlives what it points at.
    """

    MODEL_DESCRIPTION = 'One disposal within a capital gains snapshot, as it stood when taken.'

    class Meta:
        ordering = ['sell_date', 'id']

    snapshot = models.ForeignKey(
        CGTReturnSnapshot, on_delete=models.CASCADE, related_name='captured_rows')

    sell_allocation_id = models.UUIDField(
        null=True, blank=True,
        help_text='The allocation these figures came from. Deliberately not a foreign key: '
                  'the allocation may since have been replaced, which is exactly what a '
                  'snapshot is for.')
    sell_date = models.DateField(null=True, blank=True)
    instrument = models.CharField(max_length=255, blank=True)
    quantity_sold = models.DecimalField(
        max_digits=16, decimal_places=4, null=True, blank=True)
    days_held = models.IntegerField(null=True, blank=True)

    proceeds = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True)
    cost_base = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True)
    capital_gain = MoneyField(
        max_digits=19, decimal_places=4, default_currency=DEFAULT_CURRENCY,
        null=True, blank=True)

    def __str__(self):
        return f'{self.instrument} | {self.sell_date} | {self.capital_gain}'

    def as_dict(self):
        """The shape the basis change report compares against.

        Values stay as Decimal, Money and date rather than being flattened to text. The
        report converts what it needs; nothing else has to parse anything.
        """
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
    """The Consumer Price Index, one row per quarter.

    Not a `BaseModel`, and deliberately not tied to an account. CPI is a published national
    statistic, the same number for every user of this application, so hanging it off a
    portfolio would mean each portfolio carrying its own copy of a public fact and being
    able to disagree with the others about it.

    Loaded by `manage.py load_cpi`. Where a quarter is missing, indexation raises rather
    than guessing -- see `cgt.indexation`.
    """

    MODEL_DESCRIPTION = 'Consumer Price Index by quarter, used for cost base indexation.'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quarter_start_date'], name='cpi_index_keys')
        ]
        ordering = ['quarter_start_date']
        verbose_name_plural = 'CPI index'

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    quarter_start_date = models.DateField(
        help_text='First day of the quarter: 1 January, 1 April, 1 July or 1 October.')
    index_number = models.DecimalField(max_digits=12, decimal_places=4)
    source = models.CharField(
        max_length=255, blank=True,
        help_text='Where the figure came from, so a disputed cost base can be traced.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.quarter_start_date.isoformat()} | {self.index_number}'

    def clean(self):
        super().clean()
        if self.quarter_start_date is None:
            return
        if (self.quarter_start_date.month, self.quarter_start_date.day) not in (
                (1, 1), (4, 1), (7, 1), (10, 1)):
            raise ValidationError({
                'quarter_start_date':
                    'A CPI quarter starts on 1 January, 1 April, 1 July or 1 October.'})


class InstrumentValuation(BaseModel):
    """What one unit of an instrument was worth on a given day.

    **Per unit, never per parcel**, and that is the important part. A parcel is not a stable
    thing: `Parcel.bifurcate()` and `split_or_consolidate()` replace parcel rows rather than
    mutating them, so anything hung off a parcel needs hand-written code to follow it across
    a partial sale, which `CostBaseAdjustmentAllocation.bifurcate()` had to grow. A unit
    value needs none of that. `Parcel.market_value_at()` multiplies it out on demand and
    scales for any split that happened afterwards.

    One mechanism serves four different deemed disposals, which is why it is worth having a
    model rather than a special case for 2027: s112-155 (everyone, on 1 July 2027), s112-175
    (pre-CGT assets on the same date), s104-165 (leaving Australia) and s855-45 (arriving).
    Each needs the same thing -- what was this worth on that day -- and differs only in what
    is then done with the answer.
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
        Instrument, on_delete=models.CASCADE, related_name='valuations')
    valuation_date = models.DateField()
    unit_value = MoneyField(
        max_digits=19, decimal_places=6, default_currency=DEFAULT_CURRENCY)
    purpose = models.CharField(
        max_length=12, choices=ValuationPurpose.choices,
        default=ValuationPurpose.CUTOVER_2027)
    source = models.CharField(
        max_length=13, choices=ValuationSource.choices, default=ValuationSource.USER)

    def __str__(self):
        return f'{self.instrument} | {self.valuation_date.isoformat()} | {self.unit_value}'


class CapitalLossCarryForward(BaseModel):
    """A capital loss available to be applied against a later year's gains.

    Two quite different things share this model, distinguished by `is_opening_balance`.

    An **opening balance** is a loss from a return lodged before this portfolio existed in
    the application. Without somewhere to put it, every new user with any history gets a
    wrong figure on their first schedule, and there is nothing in their transactions from
    which it could be inferred. It is a number they read off their last notice of
    assessment.

    Anything else is a loss this application worked out itself for a year that has been
    closed off. It is stored rather than recomputed so that a later correction to an old
    year does not silently rewrite the losses a lodged return already relied on.
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

    def __str__(self):
        origin = 'opening balance' if self.is_opening_balance else 'calculated'
        return f'{self.fiscal_year} | {self.amount} | {origin}'

    def clean(self):
        super().clean()
        amount = getattr(self.amount, 'amount', None)
        if amount is not None and amount < 0:
            raise ValidationError({
                'amount': 'Record a loss as a positive number. A negative one here would be '
                          'applied as a gain.'})
