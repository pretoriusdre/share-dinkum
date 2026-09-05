from datetime import date, timedelta
from decimal import Decimal
import threading

from django.apps import apps
from django.core.files.temp import NamedTemporaryFile
from django.core.files.base import ContentFile
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.db import transaction
from django.db.models import Sum, Q, Max, Min
from django.forms.models import model_to_dict

from djmoney.money import Money

from share_dinkum_app import excelinterface
from share_dinkum_app import loading
from share_dinkum_app.reports import RealisedCapitalGainReport
from share_dinkum_app import cgt
from share_dinkum_app.choices import (
    AllocationMethod, LegalForm, LegalFormSource, SellStrategy,
)
from share_dinkum_app.utils import convert_to_decimal_field

from .models import BaseModel, Sell, Buy, Parcel, SellAllocation, ShareSplit, CostBaseAdjustment, CostBaseAdjustmentAllocation, DataExport, InstrumentPriceHistory, Account, ExchangeRate, Market, Instrument

import logging
logger = logging.getLogger(__name__)

_save_lock = threading.local()



@receiver(post_save, sender=Account)
def assign_default_account(sender, instance, created, **kwargs):
    if created and instance.owner.default_account is None:
        instance.owner.default_account = instance
        instance.owner.save()


@receiver(post_save, sender=Market)
def suggest_market_country(sender, instance, created, **kwargs):
    """Fill in where a market is, when it can be told from the code or suffix.

    Only on creation, and only when nothing was supplied. The country decides whether
    instruments on this market are "Australian listed" for capital gains reporting, so a
    later edit by the user must never be undone by a signal.
    """
    if not created or instance.country:
        return

    suggestion = cgt.suggested_country(instance)
    if suggestion:
        Market.objects.filter(pk=instance.pk).update(country=suggestion)
        instance.country = suggestion


@receiver(post_save, sender=Instrument)
def suggest_instrument_legal_form(sender, instance, created, **kwargs):
    """Suggest whether a well known code is a company or a trust.

    Recorded as a suggestion, never as a confirmation: the distinction is what lets a
    capital gains schedule say it is a draft. Anything the user has confirmed is left
    alone, and so is anything not on the seed list.
    """
    if not created or instance.legal_form_source == LegalFormSource.USER:
        return
    if instance.legal_form != LegalForm.UNKNOWN:
        return

    suggestion = cgt.suggest_legal_form(instance)
    if suggestion:
        Instrument.objects.filter(pk=instance.pk).update(
            legal_form=suggestion, legal_form_source=LegalFormSource.SUGGESTED)
        instance.legal_form = suggestion
        instance.legal_form_source = LegalFormSource.SUGGESTED


@receiver(post_save, sender=Buy)
def create_buy_parcel(sender, instance, created, **kwargs):

    assert isinstance(instance, Buy)

    logger.debug('Creating parcel for buy trade %s', instance)

    if not created or instance._creation_handled:
        return

    parcel = Parcel.objects.create(
        account=instance.account,
        buy=instance,
        parent_parcel=None,
        parcel_quantity=instance.quantity,
        activation_date=instance.date
    )

    message = f'This parcel was created from trade {instance}'
    parcel.log_event(message)

    instance._creation_handled = True
    instance.save(update_fields=["_creation_handled"])



@receiver(post_save, sender=Sell)
def create_sell_allocations(sender, instance, created, **kwargs):
    
    assert isinstance(instance, Sell)

    logger.debug('Creating sell allocations for %s', instance)

    if not created or instance._creation_handled:
        return

    if instance.strategy == SellStrategy.MANUAL:
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    available_parcels = Parcel.objects.filter(
        account=instance.account,
        deactivation_date__isnull=True,
        buy__instrument=instance.instrument,
        buy__date__lte=instance.date,
    )

    if instance.strategy == SellStrategy.FIFO:
        available_parcels = available_parcels.order_by('buy__date')
    elif instance.strategy == SellStrategy.LIFO:
        available_parcels = available_parcels.order_by('-buy__date')
    elif instance.strategy == SellStrategy.MIN_CGT:
        unit_proceeds = instance.unit_proceeds

        def get_unit_net_capital_gain(parcel):
            """Rank parcels by the gain per unit left after any discount.

            The discount rule comes from the cgt package rather than being applied here, so
            that parcel selection and the reports can never disagree about what a gain is
            worth. That matters more than it looks: this is the one place a discount is
            applied to a decision rather than to a figure, and the rule it uses is known to
            be wrong for a foreign or temporary resident.
            """
            capital_gain = unit_proceeds - parcel.unit_cost_base
            return cgt.apply_discount(
                capital_gain,
                purchase_date=parcel.buy.date,
                sale_date=instance.date,
                account=instance.account,
            )


        available_parcels = sorted(available_parcels, key=get_unit_net_capital_gain)

    available_parcels = [
        parcel for parcel in available_parcels
        if (parcel.remaining_quantity and parcel.remaining_quantity > 0)
    ]

    quantity_to_allocate = instance.quantity
    for parcel in available_parcels:
        parcel_quantity = parcel.parcel_quantity
        qty_for_parcel = min(parcel_quantity, quantity_to_allocate)
        SellAllocation.objects.create(
            account=instance.account,
            parcel=parcel,
            sell=instance,
            quantity=qty_for_parcel
        )
        quantity_to_allocate -= qty_for_parcel
        if quantity_to_allocate <= 0:
            break

    # mark as handled
    instance._creation_handled = True
    instance.save(update_fields=["_creation_handled"])


@receiver(post_save, sender=SellAllocation)
def handle_sell_allocation_creation(sender, instance, created, **kwargs):

    assert isinstance(instance, SellAllocation)

    if not created or instance._creation_handled:
        return
    
    logger.debug('Bifurcating parcel for sell allocation  %s', instance)

    # bifurcate the parcel
    allocated_parcel = instance.parcel.bifurcate(
        quantity=instance.quantity, date=instance.sell.date
    )
    allocated_parcel.sale_date = instance.sell.date
    allocated_parcel.save()

    # assign new parcel to allocation
    instance.parcel = allocated_parcel
    instance._creation_handled = True
    instance.save(update_fields=["parcel", "_creation_handled"])

    # update related sell totals
    instance.sell.save()


@receiver(post_delete, sender=SellAllocation)
def handle_sell_allocation_deletion(sender, instance, **kwargs):

    assert isinstance(instance, SellAllocation)

    instance.parcel.save()
    instance.sell.save()


def _fiscal_year_start(adjustment, end):
    """The first day of the year an adjustment relates to.

    Read from the account's own fiscal year configuration rather than assumed to be twelve
    months back from the end date, so a non-Australian or non-calendar year works. Computed
    arithmetically rather than through FiscalYearType.classify_date, which creates a
    FiscalYear row as a side effect -- not something a routine that only needs a date
    should be doing.

    Falls back to twelve months back where no fiscal year type is set, guarding the
    29 February case that has no counterpart in the preceding common year.
    """
    fiscal_year_type = getattr(adjustment.account, 'fiscal_year_type', None)
    if fiscal_year_type is not None:
        start_this_year = date(end.year, fiscal_year_type.start_month, fiscal_year_type.start_day)
        if end >= start_this_year:
            return start_this_year
        return date(end.year - 1, fiscal_year_type.start_month, fiscal_year_type.start_day)

    try:
        return date(end.year - 1, end.month, end.day) + timedelta(days=1)
    except ValueError:
        return date(end.year - 1, end.month, 28) + timedelta(days=1)


@receiver(post_save, sender=CostBaseAdjustment)
def allocate_cost_base_adjustment(sender, instance, created, **kwargs):
    """Spread a new adjustment across the parcels that were held during the year.

    Runs once, when the adjustment is first created. Deliberately not on every save: an
    adjustment that has already been allocated has parcels depending on those allocations,
    and re-running on an ordinary edit would silently move cost base around underneath
    figures the user may have lodged.

    The consequence is that a correction to how the weighting works does not reach
    adjustments that already exist. `manage.py reallocate_cost_base_adjustments` is how that
    is done, deliberately and with a diff, rather than as a side effect of upgrading.
    """
    assert isinstance(instance, CostBaseAdjustment)

    if not created or instance._creation_handled:
        return

    allocate_cost_base_adjustment_now(instance)


def allocate_cost_base_adjustment_now(instance):
    """The allocation itself, callable without a save.

    Separated from the signal so that re-allocating an existing adjustment is an explicit
    act with its own entry point, rather than something achieved by poking at
    `_creation_handled` and hoping the signal fires.
    """
    logger.debug('Handling cost base allocation for %s', instance)

    if instance.allocation_method != AllocationMethod.QTY_HELD:
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    end = instance.financial_year_end_date
    cutoff_date = _fiscal_year_start(instance, end)

    def days_held_in_year(parcel):
        """Days the parcel was actually held during the year the adjustment relates to.

        Bounded at both ends. Only bounding the sale side gave a parcel bought part way
        through the year a full year's weight, so a holding of two months took the same
        share per unit as one held throughout -- overstating its cost base and understating
        every gain later derived from it.
        """
        start = max(cutoff_date, parcel.buy.date)
        finish = min(end, parcel.sale_date) if parcel.sale_date else end
        return max((finish - start).days + 1, 0)

    with transaction.atomic():
        affected_parcels = list(Parcel.objects.filter(
            account=instance.account,
            buy__instrument=instance.instrument,
            deactivation_date__isnull=True,
            buy__date__lte=end
        ).filter(
            Q(sale_date__isnull=True) | Q(sale_date__gte=cutoff_date)
        ).select_related('buy'))

        total_weighted_sum = 0
        parcel_set_to_save = set()

        for parcel in affected_parcels:
            total_weighted_sum += parcel.parcel_quantity * days_held_in_year(parcel)

        if not total_weighted_sum:
            # Nothing was held during the year, so there is nothing to allocate against.
            instance._creation_handled = True
            instance.save(update_fields=["_creation_handled"])
            return

        # Largest weight last, so it can absorb the rounding residual where the fractions
        # do not divide exactly. Without this the allocations sum to slightly less than the
        # adjustment -- a few hundredths of a cent each time, but it is cost base going
        # quietly missing, and it accumulates over every adjustment a holding receives.
        weighted = sorted(
            ((parcel, parcel.parcel_quantity * days_held_in_year(parcel))
             for parcel in affected_parcels),
            key=lambda pair: pair[1],
        )

        total_adjustment = instance.cost_base_increase_converted
        amount_field = CostBaseAdjustmentAllocation._meta.get_field('cost_base_increase')
        allocated = Money(Decimal('0'), total_adjustment.currency)

        for index, (parcel, parcel_weight) in enumerate(weighted):
            is_last = index == len(weighted) - 1
            if is_last:
                # The residual, so the parts sum to the whole exactly.
                amount = total_adjustment - allocated
                adjustment_fraction = None
            else:
                adjustment_fraction = parcel_weight / total_weighted_sum
                amount = Money(
                    convert_to_decimal_field(
                        total_adjustment.amount * adjustment_fraction, amount_field),
                    total_adjustment.currency,
                )
                allocated += amount

            allocation = CostBaseAdjustmentAllocation.objects.create(
                account=instance.account,
                cost_base_increase=amount,
                parcel=parcel,
                cost_base_adjustment=instance,
                activation_date=cutoff_date
            )
            described = (
                f'residual of cost base adjustment {instance}' if adjustment_fraction is None
                else f'Added fraction {adjustment_fraction} of cost base adjustment {instance}'
            )
            allocation.log_event(described)
            parcel_set_to_save.add(parcel)

        for parcel in parcel_set_to_save:
            parcel.save()

        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])


@receiver([post_save, post_delete], sender=CostBaseAdjustmentAllocation)
def update_parcel(sender, instance, created=None, **kwargs):
    
    assert isinstance(instance, CostBaseAdjustmentAllocation)
    
    parcel = instance.parcel
    parcel.save()

    # Ensure related sell allocations recalc their cost base
    for alloc in parcel.sale_allocation.all():
        alloc.save()


@receiver(post_save, sender=ShareSplit)
def handle_share_split(sender, instance, created, **kwargs):

    assert isinstance(instance, ShareSplit)

    if not created or instance._creation_handled:
        return
    
    logger.debug('Splitting parcels as a result of %s', instance)

    with transaction.atomic():
        multiplier = instance.split_multiplier

        for parcel in Parcel.objects.filter(
            account=instance.account,
            deactivation_date__isnull=True,
            buy__instrument=instance.instrument,
            buy__date__lte=instance.date
        ):
            if not parcel.is_sold:
                new_parcel = parcel.split_or_consolidate(
                    multiplier=multiplier,
                    date=instance.date
                )
                instance.affected_parcels.add(new_parcel)

        # Mark as handled
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        instance.instrument.save() # Recalculate totals


@receiver(post_delete, sender=ShareSplit)
def remove_share_split(sender, instance, **kwargs):

    assert isinstance(instance, ShareSplit)
    
    logger.debug('Removing the applied share split %s', instance)
    
    with transaction.atomic():
        multiplier = instance.split_multiplier
        reciprocal_multiplier = 1 / multiplier

        for parcel in Parcel.objects.filter(
            account=instance.account,
            deactivation_date__isnull=True,
            buy__instrument=instance.instrument,
            buy__date__lte=instance.date
        ):
            if not parcel.is_sold:
                new_parcel = parcel.split_or_consolidate(
                    multiplier=reciprocal_multiplier,
                    date=instance.date
                )

        instance.instrument.save() # Recalculate totals



@receiver([post_save, post_delete], sender=Sell)
@receiver([post_save, post_delete], sender=Buy)
def update_instrument_position(sender, instance, **kwargs):

    assert isinstance(instance, (Buy, Sell))
    """
    Anytime a Buy or Sell is created/updated/deleted,
    refresh instrument totals.
    """

    logger.debug('Updating instrument net position after %s', instance)
    
    instrument = instance.instrument
    instrument.save(update_fields=None)  # triggers the aggregate recalculation
    logger.debug('...done')


@receiver(post_save, sender=Account)
def update_account_price_history(sender, instance, created, **kwargs):

    assert isinstance(instance, Account)

    if instance.update_price_history:
        # Ideally run this as a background task (Celery, Django-Q, etc.)
        # Exchange rates must be refreshed first. Saving an instrument stores its value converted at
        # whatever the current rate is at that moment, and nothing re-converts it afterwards, so
        # refreshing the rate second leaves every holding valued at the previous rate.
        instance.update_all_exchange_rate_history()
        instance.update_all_price_history()

        # Mark flag as cleared
        instance.update_price_history = False
        instance.save(update_fields=['update_price_history'])


@receiver(post_save, sender=DataExport)
def generate_export_file(sender, instance, created, **kwargs):

    assert isinstance(instance, DataExport)


    if instance.file:
        return  # already has a file
    logger.info('Starting data export process.')

    with NamedTemporaryFile(suffix='.xlsx') as temp_file:
        gen = excelinterface.ExcelGen(title='Data Export')
        for model in apps.get_app_config('share_dinkum_app').get_models():

            if model == InstrumentPriceHistory and not instance.include_price_history:
                continue

            logger.info('    - %s', model.__name__)

            if 'account' in [f.name for f in model._meta.get_fields()]:
                queryset = loading.model_to_queryset(model=model, account=instance.account)
            else:
                queryset = loading.model_to_queryset(model=model)
            
            df = loading.queryset_to_df(queryset)
            desc = getattr(model, 'MODEL_DESCRIPTION', 'No description available')
            if not df.empty:
                gen.add_table(df, table_name=model.__name__, description=desc)

        logger.info('    - Realised Capital Gains Report')
        rcg_report = RealisedCapitalGainReport(account=instance.account)
        
        df_realised_capital_gains = rcg_report.generate()
        
        gen.add_table(df_realised_capital_gains, table_name="RealisedCapitalGains", description="Report of realised capital gains per sale allocation.")


        gen.save(temp_file.name)
        new_name = f'Export_{instance.account.description}.xlsx'
        instance.file.save(new_name, ContentFile(open(temp_file.name, 'rb').read()))
        logger.info('Data export process completed successfully.')



@receiver(post_delete)
def delete_file_on_delete(sender, instance, **kwargs):
    """
    Deletes the file when a model instance is deleted.
    Only works if the file field is named 'file'.
    """
    file_field = getattr(instance, 'file', None)
    if file_field:
        file_field.delete(save=False)


@receiver(pre_save)
def delete_file_on_change(sender, instance, **kwargs):
    """
    Deletes old file if the 'file' field is updated.
    """
    if not instance.pk:
        return  # New instance, nothing to delete

    try:
        old_instance = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    old_file = getattr(old_instance, 'file', None)
    new_file = getattr(instance, 'file', None)

    if old_file and old_file != new_file:
        old_file.delete(save=False)



@receiver(post_save)
def persist_safe_properties(sender, instance, created, **kwargs):
    logger.debug('Setting calculated fields for %s', instance)
    logger.debug('Instance data is: %s', model_to_dict(instance))

    # Prevent recursion
    if getattr(_save_lock, "active", False):
        return

    # Only act on subclasses of BaseModel
    if not isinstance(instance, BaseModel):
        return

    updated_fields = []

    needs_exchange_rate = False
    currency_val = None
    if hasattr(instance, 'exchange_rate'):
        for attr_name in dir(instance):
            if attr_name.endswith('_currency'):
                try:
                    val = getattr(instance, attr_name, None)
                    if (
                        val and
                        val != instance.account.currency and not
                        getattr(instance, 'exchange_rate', None)
                    ):
                        needs_exchange_rate = True
                        currency_val = val
                        break
                except AttributeError:
                    continue

    if needs_exchange_rate:
        exchange_rate_obj = ExchangeRate.get_or_create(
            account=instance.account,
            convert_from=currency_val,
            convert_to=instance.account.currency,
            exchange_date=getattr(instance, 'date', None),
        )
        instance.exchange_rate = exchange_rate_obj
        updated_fields.append('exchange_rate')


    for attr_name in dir(instance):
        
        if attr_name  == 'value_held_converted':
            value = getattr(instance, attr_name)

        attr = getattr(type(instance), attr_name, None)
        try:
            val = getattr(instance, attr_name, None)
            # --- Step 2: safe_property fields ---
            if isinstance(attr, property) and getattr(attr.fget, "_is_safe_property", False):
                value = getattr(instance, attr_name)

                calc_field_name = f"calculated_{attr_name}"
                if hasattr(instance, calc_field_name):
                    setattr(instance, calc_field_name, value)
                    updated_fields.append(calc_field_name)
                    if isinstance(value, Money):
                        updated_fields.append(f"{calc_field_name}_currency")

        except AttributeError:
            continue

    # Save once if anything was updated
    if updated_fields:
        with transaction.atomic():
            _save_lock.active = True
            try:
                instance.save(update_fields=updated_fields)
            finally:
                    _save_lock.active = False

    logger.debug('Updated instance data is: %s', model_to_dict(instance))