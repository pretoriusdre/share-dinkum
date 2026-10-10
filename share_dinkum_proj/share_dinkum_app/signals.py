from collections.abc import Iterable
from datetime import date
from decimal import Decimal
import threading
from typing import Any, cast

from django.db.models.signals import pre_save, post_save, pre_delete, post_delete
from django.dispatch import receiver
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Model, Q
from django.db.models.fields.files import FieldFile
from django.forms.models import model_to_dict

from djmoney.models.fields import CurrencyField, MoneyField
from djmoney.money import Money

from share_dinkum_app import portfolio_export
from share_dinkum_app import cgt
from share_dinkum_app.holdings import live, strategies
from share_dinkum_app.choices import (
    AllocationMethod, LegalForm, LegalFormSource, SellStrategy,
)
from share_dinkum_app.utils import convert_to_decimal_field

from .models import BaseModel, Sell, Buy, Parcel, SellAllocation, ShareSplit, CostBaseAdjustment, CostBaseAdjustmentAllocation, DataExport, Account, ExchangeRate, Market, Instrument

import logging
logger = logging.getLogger(__name__)

_save_lock = threading.local()



@receiver(post_save, sender=Account)
def assign_default_account(sender: type[Model], instance: Account, created: bool, **kwargs: Any) -> None:
    if created and instance.owner.default_account is None:
        instance.owner.default_account = instance
        instance.owner.save()


@receiver(post_save, sender=Market)
def suggest_market_country(sender: type[Model], instance: Market, created: bool, **kwargs: Any) -> None:
    """On creation with no country, suggest one from the market's code or suffix."""
    if not created or instance.country:
        return

    suggestion = cgt.suggested_country(instance)
    if suggestion:
        Market.objects.filter(pk=instance.pk).update(country=suggestion)
        instance.country = suggestion


@receiver(post_save, sender=Instrument)
def suggest_instrument_legal_form(sender: type[Model], instance: Instrument, created: bool, **kwargs: Any) -> None:
    """On creation with an unknown legal form, suggest one for a known code.

    Saved with source SUGGESTED, so it does not count as confirmed.
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
def create_buy_parcel(sender: type[Model], instance: Buy, created: bool, **kwargs: Any) -> None:

    if live.active():
        return  # The replay builds the holding (holdings/live.py).

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
def create_sell_allocations(sender: type[Model], instance: Sell, created: bool, **kwargs: Any) -> None:
    
    if live.active():
        return  # The replay builds the holding (holdings/live.py).

    assert isinstance(instance, Sell)

    logger.debug('Creating sell allocations for %s', instance)

    if not created or instance._creation_handled:
        return

    if instance.strategy == SellStrategy.MANUAL:
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    candidates = list(Parcel.objects.filter(
        account=instance.account,
        deactivation_date__isnull=True,
        buy__instrument=instance.instrument,
        buy__date__lte=instance.date,
    ).select_related('buy'))

    net_gain_per_unit = None
    if instance.strategy == SellStrategy.MIN_CGT:
        unit_proceeds = instance.unit_proceeds

        def net_gain_per_unit(parcel: Parcel) -> Any:
            """Per-unit gain after discount, using the same rule as the reports."""
            capital_gain = unit_proceeds - parcel.unit_cost_base
            return cgt.apply_discount(
                capital_gain,
                purchase_date=parcel.buy.date,
                sale_date=instance.date,
                account=instance.account,
            )

    ordered = strategies.order_for_sale(
        instance.strategy, candidates, buy_date=lambda parcel: parcel.buy.date,
        tie=lambda parcel: str(parcel.pk), net_gain_per_unit=net_gain_per_unit)

    taken, quantity_to_allocate = strategies.take(instance.quantity, (
        (parcel, parcel.parcel_quantity) for parcel in ordered
        if parcel.remaining_quantity and parcel.remaining_quantity > 0))
    for parcel, quantity in taken:
        SellAllocation.objects.create(
            account=instance.account,
            parcel=parcel,
            sell=instance,
            quantity=quantity
        )

    if quantity_to_allocate > 0:
        # Not an error, since the sale may be recorded before the purchase it draws on, but
        # until it is allocated the gain on these units is in no report.
        logger.warning(
            '%s units of %s had no parcel to be allocated to. Their gain is not reported '
            'until they are.', quantity_to_allocate, instance)

    # mark as handled
    instance._creation_handled = True
    instance.save(update_fields=["_creation_handled"])


@receiver(post_save, sender=SellAllocation)
def handle_sell_allocation_creation(sender: type[Model], instance: SellAllocation, created: bool, **kwargs: Any) -> None:

    if live.active():
        return  # The replay builds the holding (holdings/live.py).

    assert isinstance(instance, SellAllocation)

    if not created or instance._creation_handled:
        return
    
    logger.debug('Bifurcating parcel for sell allocation  %s', instance)

    # bifurcate the parcel
    allocated_parcel = instance.parcel.bifurcate(
        quantity=instance.quantity, date=instance.sell.date
    )
    allocated_parcel.sale_date = instance.sell.date

    # assign new parcel to allocation
    instance.parcel = allocated_parcel
    instance._creation_handled = True
    instance.save(update_fields=["parcel", "_creation_handled"])

    # Saved after the allocation points at it, so its stored figures count the sale. Saved
    # before, it recorded itself as unsold, and nothing saved it again.
    allocated_parcel.save()

    # update related sell totals
    instance.sell.save()


@receiver(post_delete, sender=SellAllocation)
def handle_sell_allocation_deletion(sender: type[Model], instance: SellAllocation, **kwargs: Any) -> None:

    if live.active():
        return  # The replay builds the holding (holdings/live.py).

    assert isinstance(instance, SellAllocation)

    parcel = instance.parcel
    if not parcel.sale_allocation.filter(is_active=True).exists():
        # Unsold again. A sale date left behind would have a later cost base adjustment
        # weight the parcel as if it had been sold that day.
        parcel.sale_date = None
    parcel.save()
    instance.sell.save()


def _fiscal_year_start(adjustment: CostBaseAdjustment, end: date) -> date:
    """The start of the fiscal year containing `end`, from the account's fiscal year type.

    Without one, the day after `end` a year earlier. Avoids `classify_date`, which creates
    a FiscalYear row.
    """
    fiscal_year_type = getattr(adjustment.account, 'fiscal_year_type', None)
    if fiscal_year_type is not None:
        return strategies.fiscal_year_start(end, fiscal_year_type.start_month, fiscal_year_type.start_day)
    return strategies.fiscal_year_start(end)


@receiver(post_save, sender=CostBaseAdjustment)
def allocate_cost_base_adjustment(sender: type[Model], instance: CostBaseAdjustment, created: bool, **kwargs: Any) -> None:
    """Allocate a new adjustment across the parcels held during its year.

    Runs on creation only, so editing an adjustment never moves existing allocations.
    """
    assert isinstance(instance, CostBaseAdjustment)

    if live.active() or not created or instance._creation_handled:
        return

    allocate_cost_base_adjustment_now(instance)


def allocate_cost_base_adjustment_now(instance: CostBaseAdjustment) -> None:
    """Allocate an adjustment across parcels, weighted by quantity times days held in the year.

    Only for the QTY_HELD method. The largest weight takes the rounding residual, so the
    allocations sum exactly to the adjustment.
    """
    logger.debug('Handling cost base allocation for %s', instance)

    if instance.allocation_method != AllocationMethod.QTY_HELD:
        instance._creation_handled = True
        instance.save(update_fields=["_creation_handled"])
        return

    end = instance.financial_year_end_date
    cutoff_date = _fiscal_year_start(instance, end)

    splits = [(split.date, split.ratio) for split in ShareSplit.objects.filter(
        account=instance.account, instrument=instance.instrument, is_active=True)]

    def weight(parcel: Parcel) -> Decimal:
        return strategies.holding_weight(
            parcel.parcel_quantity, parcel.cumulative_split_multiplier, parcel.buy.date,
            parcel.sale_date, cutoff_date, end, splits)

    with transaction.atomic():
        affected_parcels = list(Parcel.objects.filter(
            account=instance.account,
            buy__instrument=instance.instrument,
            deactivation_date__isnull=True,
            buy__date__lte=end
        ).filter(
            Q(sale_date__isnull=True) | Q(sale_date__gte=cutoff_date)
        ).select_related('buy'))

        total_adjustment = instance.cost_base_increase_converted
        amount_field = CostBaseAdjustmentAllocation._meta.get_field('cost_base_increase')
        parts = strategies.spread(
            total_adjustment.amount, ((parcel, weight(parcel)) for parcel in affected_parcels),
            quantize=lambda amount: cast(Decimal, convert_to_decimal_field(amount, amount_field)))

        if not parts:
            # Nothing was held during the year, so there is nothing to allocate against.
            instance._creation_handled = True
            instance.save(update_fields=["_creation_handled"])
            return

        parcel_set_to_save: set[Parcel] = set()
        for parcel, amount, adjustment_fraction in parts:
            allocation = CostBaseAdjustmentAllocation.objects.create(
                account=instance.account,
                cost_base_increase=Money(amount, total_adjustment.currency),
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
def update_parcel(sender: type[Model], instance: CostBaseAdjustmentAllocation, created: bool | None = None,
                  **kwargs: Any) -> None:
    
    assert isinstance(instance, CostBaseAdjustmentAllocation)
    
    parcel = instance.parcel
    parcel.save()

    # Ensure related sell allocations recalc their cost base
    for alloc in parcel.sale_allocation.all():
        alloc.save()


@receiver(post_save, sender=ShareSplit)
def handle_share_split(sender: type[Model], instance: ShareSplit, created: bool, **kwargs: Any) -> None:

    assert isinstance(instance, ShareSplit)

    if live.active() or not created or instance._creation_handled:
        return
    
    logger.debug('Splitting parcels as a result of %s', instance)

    with transaction.atomic():
        multiplier = instance.ratio

        # The split is dated on its ex-date. A buy on that day is already in post-split units.
        for parcel in Parcel.objects.filter(
            account=instance.account,
            deactivation_date__isnull=True,
            buy__instrument=instance.instrument,
            buy__date__lt=instance.date
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


@receiver(pre_delete, sender=ShareSplit)
def remove_share_split(sender: type[Model], instance: ShareSplit, **kwargs: Any) -> None:
    """Reverse the split on the parcels it created, before the split is deleted.

    A pre_delete, because by post_delete the `affected_parcels` rows are already gone. Only
    those parcels are reversed: going by date would also reverse parcels the split never
    touched. Refused once any of them has been sold or split again.
    """
    assert isinstance(instance, ShareSplit)

    blocker = instance.deletion_blocker()
    if blocker:
        raise ValueError(blocker)
    if live.active():
        return  # The rebuild after the delete drops the parcels it made.

    logger.debug('Removing the applied share split %s', instance)

    with transaction.atomic():
        reciprocal_multiplier = 1 / instance.ratio

        for parcel in instance.affected_parcels.all():
            parcel.split_or_consolidate(
                multiplier=reciprocal_multiplier,
                date=instance.date
            )

        instance.instrument.save() # Recalculate totals



@receiver([post_save, post_delete], sender=Sell)
@receiver([post_save, post_delete], sender=Buy)
def update_instrument_position(sender: type[Model], instance: Buy | Sell, **kwargs: Any) -> None:

    assert isinstance(instance, (Buy, Sell))
    """
    Anytime a Buy or Sell is created/updated/deleted,
    refresh instrument totals.
    """

    # Inside another record's re-save of its stored figures, `_save_lock` stops this save
    # working its figures out, so it would write back whatever the cached instrument held,
    # undoing a fresher save. The outer save refreshes it.
    if getattr(_save_lock, "active", False):
        return

    logger.debug('Updating instrument net position after %s', instance)
    # Read again rather than saving the cached instance, whose figures may be out of date.
    instrument = Instrument.objects.get(pk=instance.instrument_id)
    instrument.save(update_fields=None)  # triggers the aggregate recalculation
    instance.instrument = instrument
    logger.debug('...done')


@receiver(post_save, sender=Account)
def update_account_price_history(sender: type[Model], instance: Account, created: bool, **kwargs: Any) -> None:

    assert isinstance(instance, Account)

    # Deprecated: the dashboard calls `refresh_market_data` directly. Kept for anyone who
    # ticks the box in the admin.
    if instance.update_price_history:
        instance.refresh_market_data()


@receiver(post_save, sender=DataExport)
def generate_export_file(sender: type[Model], instance: DataExport, created: bool, **kwargs: Any) -> None:
    """Write the file for a new export: the admin's add button, and scheduled exports."""
    assert isinstance(instance, DataExport)
    portfolio_export.create_export(instance)


def _delete_file_after_commit(field_file: FieldFile) -> None:
    """Delete a stored file once the surrounding transaction commits.

    Deleting it straight away lost the document whenever the change was then rolled back
    (a failed import, say): the row went back to naming a file that was gone.
    """
    storage, name = field_file.storage, cast(str, field_file.name)
    transaction.on_commit(lambda: storage.delete(name))


def _has_file_field(model: type[Model]) -> bool:
    return any(field.name == 'file' for field in model._meta.fields)


@receiver(post_delete)
def delete_file_on_delete(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Delete a deleted instance's `file`, for any model with a field of that name."""
    file_field = getattr(instance, 'file', None)
    if file_field and _has_file_field(sender):
        _delete_file_after_commit(file_field)


@receiver(pre_save)
def delete_file_on_change(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Delete the old `file` when a saved instance's `file` changes."""
    if not _has_file_field(sender):
        return
    if not instance.pk or instance._state.adding:
        return  # New instance, nothing to delete

    try:
        old_instance = sender._default_manager.get(pk=instance.pk)
    except ObjectDoesNotExist:
        return

    old_file = getattr(old_instance, 'file', None)
    new_file = getattr(instance, 'file', None)

    if old_file and old_file != new_file:
        _delete_file_after_commit(old_file)


def _money_fields(model: type[Model]) -> list[Any]:
    """The record's own money fields, in declaration order. The calculated_ copies are the
    application's output, already converted."""
    return [
        field for field in model._meta.fields
        if isinstance(field, MoneyField) and not field.name.startswith('calculated_')]


def _date_field_name(model: type[Model]) -> str | None:
    names = {field.name for field in model._meta.fields}
    for name in ('date', 'financial_year_end_date'):
        if name in names:
            return name
    return None


def _record_currency(instance: Model) -> str | None:
    """The currency the record's amounts are in, or None if it has none.

    The first amount that is not zero decides. A field left at zero keeps the column default
    (AUD) whatever the record is in, so it would name the wrong currency. Only when every
    amount is zero does the first field decide: a worthless sale still needs its zero
    proceeds converted, or they cannot be set against a cost base.
    """
    amounts: list[Any] = [getattr(instance, field.name, None) for field in _money_fields(type(instance))]
    amounts = [money for money in amounts if money is not None]
    for money in amounts:
        if money.amount:
            return str(money.currency)
    return str(amounts[0].currency) if amounts else None


def _saves_conversion_inputs(model: type[Model], update_fields: Iterable[str]) -> bool:
    """Whether a save limited to `update_fields` writes anything the rate depends on."""
    inputs: set[str | None] = {'exchange_rate', _date_field_name(model)}
    for field in _money_fields(model):
        inputs |= {field.name, f'{field.name}_currency'}
    return bool(set(update_fields) & inputs)


def _moved_off_rate_date(instance: Model, rate: ExchangeRate) -> bool:
    """Whether this save moves a stored record off the date its rate was fetched for.

    A rate the user chose for another date on purpose is left alone: only a rate for the date
    the record had before this save is taken to be the one it was given automatically.
    """
    name = _date_field_name(type(instance))
    if name is None or instance._state.adding or instance.pk is None:
        return False
    stored = type(instance)._default_manager.filter(pk=instance.pk).values_list(name, flat=True).first()
    return stored is not None and stored != getattr(instance, name) and rate.date == stored


@receiver(pre_save)
def attach_exchange_rate(sender: type[Model], instance: Model, raw: bool = False,
                         update_fields: Iterable[str] | None = None, **kwargs: Any) -> None:
    """Give a foreign-currency record the exchange rate for its currency and date.

    This has to happen before post_save: the parcel, sell allocations and cost base
    allocations are built from the record by post_save handlers, and they would otherwise
    see its amounts unconverted.

    A rate already attached is kept unless it no longer fits: it converts another currency
    than the record is now in (applying it would fail), or the record has moved off the date
    it was fetched for. A dividend's date and currency can be corrected after it is entered.
    """
    if raw or not isinstance(instance, BaseModel) or not hasattr(instance, 'exchange_rate'):
        return
    record: Any = instance  # has `exchange_rate`, which only some BaseModels do
    if update_fields is not None and not _saves_conversion_inputs(sender, update_fields):
        # Storing calculated figures, say. A rate changed here would not even be written.
        return

    account_currency = str(instance.account.currency)
    currency = _record_currency(instance)
    wanted = currency if currency and currency != account_currency else None

    rate = record.exchange_rate
    if rate is not None and wanted is not None:
        fits = (str(rate.convert_from), str(rate.convert_to)) == (wanted, account_currency)
        if fits and not _moved_off_rate_date(instance, rate):
            return

    record.exchange_rate = None if wanted is None else ExchangeRate.get_or_create(
        account=instance.account,
        convert_from=wanted,
        convert_to=account_currency,
        exchange_date=cast(date, getattr(instance, 'date', None)),
    )


#: A stored figure whose property is not named after it. Otherwise `calculated_X` copies `X`.
CALCULATED_SOURCES = {'calculated_affected_parcels': 'affected_parcel_list'}

_calculated_sources: dict[type[Model], list[tuple[str, str]]] = {}


def calculated_sources(model: type[Model]) -> list[tuple[str, str]]:
    """`(stored field, safe property)` for each of the model's `calculated_*` fields.

    Worked out once per model. In property name order, as `dir()` gave them before.
    """
    sources = _calculated_sources.get(model)
    if sources is None:
        pairs: list[tuple[str, str]] = []
        for field in model._meta.concrete_fields:
            if not field.name.startswith('calculated_') or isinstance(field, CurrencyField):
                continue
            source = CALCULATED_SOURCES.get(field.name, field.name.removeprefix('calculated_'))
            attr = getattr(model, source, None)
            if isinstance(attr, property) and getattr(attr.fget, '_is_safe_property', False):
                pairs.append((field.name, source))
        sources = _calculated_sources[model] = sorted(pairs, key=lambda pair: pair[1])
    return sources


#: Per fact, the fields a holding is worked out from. Changing one rebuilds the instrument.
HOLDINGS_FACT_FIELDS: dict[type[Model], tuple[str, ...]] = {
    Buy: ('instrument', 'date', 'quantity'),
    Sell: ('instrument', 'date', 'quantity', 'strategy'),
    SellAllocation: ('parcel', 'sell', 'quantity', 'is_active'),
    ShareSplit: ('instrument', 'date', 'quantity_before', 'quantity_after', 'is_active'),
    CostBaseAdjustment: ('instrument', 'financial_year_end_date', 'cost_base_increase',
                         'cost_base_increase_currency', 'allocation_method', 'exchange_rate'),
    CostBaseAdjustmentAllocation: ('parcel', 'cost_base_adjustment', 'cost_base_increase', 'deactivation_date'),
}


def _fact_changed(sender: type[Model], instance: Model, stored: Model) -> bool:
    for name in HOLDINGS_FACT_FIELDS[sender]:
        field: Any = sender._meta.get_field(name)
        if field.to_python(getattr(stored, field.attname)) != field.to_python(getattr(instance, field.attname)):
            return True
    return False


@receiver(pre_save)
def note_holdings_change(sender: type[Model], instance: Model, raw: bool = False, **kwargs: Any) -> None:
    """Before a fact is saved: which instruments it changes, checked against their trades first."""
    if raw or sender not in HOLDINGS_FACT_FIELDS or not live.active() or live.rebuilding():
        return
    stored = None
    if instance._state.adding:
        if getattr(instance, '_creation_handled', False):
            return  # From an export, with its parcels beside it; the loader rebuilds after.
    else:
        stored = sender._default_manager.filter(pk=instance.pk).first()
        if stored is not None and not _fact_changed(sender, instance, stored):
            return
    candidates = [live.instrument_of(instance), live.instrument_of(stored) if stored is not None else None]
    instruments = {instrument for instrument in candidates if instrument is not None}
    for instrument in instruments:
        live.ensure_verified(instrument)
    instance._holdings_instruments = instruments  # type: ignore[attr-defined]


@receiver(pre_delete)
def note_holdings_deletion(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Before a fact is deleted. Deleting an allocation of an automatic sale makes the sale
    MANUAL: left automatic, the rebuild would allocate it again straight away."""
    if sender not in HOLDINGS_FACT_FIELDS or not live.active() or live.rebuilding():
        return
    instrument = live.instrument_of(instance)
    if instrument is None:
        return
    live.ensure_verified(instrument)
    if isinstance(instance, SellAllocation) and instance.sell.strategy != SellStrategy.MANUAL:
        # Strategy is structural, so a save refuses it; this is the user's own decision.
        Sell.objects.filter(pk=instance.sell_id).update(strategy=SellStrategy.MANUAL)
    instance._holdings_instruments = {instrument}  # type: ignore[attr-defined]


@receiver(post_save)
@receiver(post_delete)
def rebuild_holdings(sender: type[Model], instance: Model, created: bool = False, **kwargs: Any) -> None:
    """After a fact is saved or deleted: rebuild what it changed, in the same transaction."""
    instruments = getattr(instance, '_holdings_instruments', None)
    if not instruments:
        return
    instance._holdings_instruments = None  # type: ignore[attr-defined]
    for instrument in instruments:
        live.rebuild(instrument)
    if created and getattr(instance, '_creation_handled', True) is False:
        sender._default_manager.filter(pk=instance.pk).update(_creation_handled=True)
        instance._creation_handled = True  # type: ignore[attr-defined]


@receiver(post_save)
def persist_safe_properties(sender: type[Model], instance: Model, created: bool, **kwargs: Any) -> None:
    """Copy each safe property to its `calculated_*` field, and save them in one more save.

    That save runs with `_save_lock` set, so it does not come back here, and nor does any save
    it causes.
    """
    if getattr(_save_lock, "active", False) or not isinstance(instance, BaseModel):
        return

    debug = logger.isEnabledFor(logging.DEBUG)
    if debug:
        logger.debug('Setting calculated fields for %s: %s', instance, model_to_dict(instance))

    updated_fields: list[str] = []
    for field_name, source in calculated_sources(type(instance)):
        try:
            value = getattr(instance, source)
        except AttributeError:
            continue  # A property that cannot be worked out yet, e.g. a missing relation.
        setattr(instance, field_name, value)
        updated_fields.append(field_name)
        if isinstance(value, Money):
            updated_fields.append(f"{field_name}_currency")

    if updated_fields:
        with transaction.atomic():
            _save_lock.active = True
            try:
                instance.save(update_fields=updated_fields)
            finally:
                _save_lock.active = False

    if debug:
        logger.debug('Updated instance data is: %s', model_to_dict(instance))