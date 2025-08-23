from datetime import date, timedelta

from django.apps import apps

from django.core.files.temp import NamedTemporaryFile
from django.core.files.base import ContentFile

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import BaseModel, Sell, Buy, Parcel, SellAllocation, ShareSplit, CostBaseAdjustment, CostBaseAdjustmentAllocation, DataExport, InstrumentPriceHistory, Account, ExchangeRate

from django.db import transaction

from share_dinkum_app import excelinterface
from share_dinkum_app import loading

from django.db.models import Sum, Q, Max, Min


import threading


import logging
logger = logging.getLogger(__name__)

_save_lock = threading.local()



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

    if instance.strategy == 'MANUAL':
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    available_parcels = Parcel.objects.filter(
        account=instance.account,
        deactivation_date__isnull=True,
        buy__instrument=instance.instrument,
        buy__date__lte=instance.date,
    )

    if instance.strategy == 'FIFO':
        available_parcels = available_parcels.order_by('buy__date')
    elif instance.strategy == 'LIFO':
        available_parcels = available_parcels.order_by('-buy__date')
    elif instance.strategy == 'MIN_CGT':
        unit_proceeds = instance.unit_proceeds

        def get_unit_net_capital_gain(parcel):
            capital_gain = unit_proceeds - parcel.unit_cost_base
            if (instance.date - parcel.buy.date).days > 365:
                capital_gain *= 0.5
            return capital_gain

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



@receiver(post_save, sender=ShareSplit)
def handle_share_split(sender, instance, created, **kwargs):

    assert isinstance(instance, ShareSplit)

    if not created or instance._creation_handled:
        return

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



@receiver(post_save, sender=CostBaseAdjustment)
def handle_cost_base_allocation(sender, instance, created, **kwargs):

    assert isinstance(instance, CostBaseAdjustment)

    if not created or instance._creation_handled:
        return

    if instance.allocation_method != 'QTY_HELD':
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    end = instance.financial_year_end_date
    cutoff_date = date(end.year - 1, end.month, end.day) + timedelta(days=1)

    with transaction.atomic():
        affected_parcels = list(Parcel.objects.filter(
            account=instance.account,
            buy__instrument=instance.instrument,
            deactivation_date__isnull=True,
            buy__date__lte=end
        ).filter(
            Q(sale_date__isnull=True) | Q(sale_date__gte=cutoff_date)
        ))

        total_weighted_sum = 0
        days_in_year = (end - cutoff_date).days + 1
        parcel_set_to_save = set()

        for parcel in affected_parcels:
            days_held = min(days_in_year, (parcel.sale_date - cutoff_date).days + 1) if parcel.sale_date else days_in_year
            total_weighted_sum += parcel.parcel_quantity * days_held

        for parcel in affected_parcels:
            days_held = min(days_in_year, (parcel.sale_date - cutoff_date).days + 1) if parcel.sale_date else days_in_year
            parcel_weight = parcel.parcel_quantity * days_held
            adjustment_fraction = parcel_weight / total_weighted_sum

            allocation = CostBaseAdjustmentAllocation.objects.create(
                account=instance.account,
                cost_base_increase=instance.cost_base_increase_converted * adjustment_fraction,
                parcel=parcel,
                cost_base_adjustment=instance,
                activation_date=cutoff_date
            )
            allocation.log_event(f'Added fraction {adjustment_fraction} of cost base adjustment {instance}')
            parcel_set_to_save.add(parcel)

        for parcel in parcel_set_to_save:
            parcel.save()

        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])



@receiver([post_save, post_delete], sender=Sell)
@receiver([post_save, post_delete], sender=Buy)
def update_instrument_position(sender, instance, **kwargs):

    assert isinstance(instance, (Buy, Sell))
    """
    Anytime a Buy or Sell is created/updated/deleted,
    refresh instrument totals.
    """
    instrument = instance.instrument
    instrument.save(update_fields=None)  # triggers the aggregate recalculation


@receiver(post_save, sender=Account)
def update_account_price_history(sender, instance, created, **kwargs):

    assert isinstance(instance, Account)

    if instance.update_price_history:
        # Ideally run this as a background task (Celery, Django-Q, etc.)
        instance.update_all_price_history()
        instance.update_all_exchange_rate_history()

        # Mark flag as cleared
        instance.update_price_history = False
        instance.save(update_fields=['update_price_history'])

@receiver(post_save, sender=DataExport)
def generate_export_file(sender, instance, created, **kwargs):

    assert isinstance(instance, DataExport)

    if instance.file:
        return  # already has a file

    with NamedTemporaryFile(suffix='.xlsx') as temp_file:
        gen = excelinterface.ExcelGen(title='Data Export')
        for model in apps.get_app_config('share_dinkum_app').get_models():
            if model == InstrumentPriceHistory and not instance.include_price_history:
                continue
            if 'account' in [f.name for f in model._meta.get_fields()]:
                queryset = loading.model_to_queryset(model=model, account=instance.account)
            else:
                queryset = loading.model_to_queryset(model=model)
            
            df = loading.queryset_to_df(queryset)
            desc = getattr(model, 'MODEL_DESCRIPTION', 'No description available')
            if not df.empty:
                gen.add_table(df, table_name=model.__name__, description=desc)

        gen.save(temp_file.name)
        new_name = f'Export_{instance.account.description}.xlsx'
        instance.file.save(new_name, ContentFile(open(temp_file.name, 'rb').read()))





@receiver(post_save)
def persist_safe_properties(sender, instance, created, **kwargs):

    logger.debug('Setting calculated fields for %s', instance)


    # Prevent recursion
    if getattr(_save_lock, "active", False):
        return
    
    # Only act on subclasses of BaseModel
    if not isinstance(instance, BaseModel):
        return

    updated_fields = []

    for attr_name in dir(instance):
        if attr_name.startswith('_'):
            continue

        try:
            attr = getattr(type(instance), attr_name, None)
            # Check if this is a safe_property
            if isinstance(attr, property) and getattr(attr.fget, "_is_safe_property", False):
                
                value = getattr(instance, attr_name)

                #print(attr_name, value, type(value))

                calc_field_name = f"calculated_{attr_name}"
                if hasattr(instance, calc_field_name):
                    setattr(instance, calc_field_name, value)
                    updated_fields.append(calc_field_name)

            # Handle currency -> exchange_rate
            if attr_name.endswith('_currency') and hasattr(instance, 'exchange_rate'):
                val = getattr(instance, attr_name, None)
                if val and val != instance.account.currency and not getattr(instance, 'exchange_rate', None):
                    exchange_rate_obj = ExchangeRate.get_or_create(
                        account=instance.account,
                        convert_from=val,
                        convert_to=instance.account.currency,
                        exchange_date=getattr(instance, 'date', None)
                    )
                    instance.exchange_rate = exchange_rate_obj
                    updated_fields.append('exchange_rate')

        except AttributeError:
            continue

    if updated_fields:
        with transaction.atomic():
            _save_lock.active = True
            instance.save(update_fields=updated_fields)
            _save_lock.active = False