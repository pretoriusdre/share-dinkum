"""CGT events: one flat row per capital gain or loss, which every CGT report reads from.

Sources:
* a disposal (one SellAllocation), split into two rows by s112-155 when a parcel held
  across 1 July 2027 is sold after it and the 2027 regime is modelled
* a gain attributed by a managed investment trust's annual statement
"""

from dataclasses import dataclass, fields, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any, cast, overload

from django.db.models import Sum
from djmoney.money import Money

from share_dinkum_app.cgt import (
    classification,
    cutover as cutover_module,
    discount as discount_module,
    indexation as indexation_module,
    residency as residency_module,
    tap as tap_module,
)
from share_dinkum_app import constants
from share_dinkum_app.constants import (
    CGT_CUTOVER_DATE,
    CGT_GAIN_DEFERRED_NON_RESIDENTIAL,
    CGT_GAIN_NON_RESIDENTIAL,
    CGT_GAIN_RESIDENTIAL,
)

if TYPE_CHECKING:
    from share_dinkum_app.models import (
        Account, AttributionStatement, FiscalYear, Instrument, Parcel, Buy, Sell, SellAllocation,
    )

#: A disposal of a parcel: one SellAllocation.
SOURCE_DISPOSAL = 'disposal'

#: A capital gain attributed by a managed investment trust, from its annual statement.
SOURCE_ATTRIBUTION = 'trust_attribution'

#: TAP status values, re-exported from cgt.tap for reports.
TAP = tap_module.TAP
NTAP = tap_module.NTAP

#: The gain is eligible for the CGT discount.
METHOD_DISCOUNT = 'discount'

#: No discount: held 12 months or less, indexed, post-cutover, or a trust's other-method gain.
METHOD_OTHER = 'other'

REGIME_PRE_CUTOVER = 'pre_2027'
REGIME_POST_CUTOVER = 'post_2027'

#: A disposal not split by s112-155.
SLICE_WHOLE = 'whole'

#: The two halves of a disposal split by s112-155. They share a sell allocation id and,
#: before indexation, sum to the whole gain.
SLICE_PRE_CUTOVER = 'pre_cutover'
SLICE_POST_CUTOVER = 'post_cutover'

#: No indexation applied. Distinct from 1.000, which means indexed with flat CPI.
NO_INDEXATION = None


@dataclass(frozen=True)
class CGTEvent:
    """One capital gains event, with everything needed to characterise and trace it.

    Frozen, so reports cannot alter figures.
    """

    # --- what and when -----------------------------------------------------
    source: str
    event_date: date
    fiscal_year: str | None
    instrument: str
    #: The CGT schedule category, from the instrument's legal form and market.
    asset_category: str
    #: False if the instrument's legal form is not confirmed by the user.
    asset_category_confirmed: bool

    # --- traceability, so any figure can be followed back to its records ----
    #: Primary key values, not strings.
    sell_allocation_id: object | None
    parcel_id: object | None
    buy_id: object | None
    sell_id: object | None
    legacy_buy_id: str | None
    legacy_sell_id: str | None

    # --- holding period ----------------------------------------------------
    purchase_date: date | None
    sale_date: date | None
    quantity: Decimal | None
    days_held: int | None

    # --- cost base build-up, which must sum to cost_base --------------------
    unit_buy_price: Money | None
    buy_consideration: Money | None
    buy_brokerage: Money | None
    cost_base_adjustments: Money | None
    cost_base: Money | None

    # --- proceeds ----------------------------------------------------------
    unit_sale_price: Money | None
    gross_proceeds: Money | None
    sell_brokerage: Money | None
    net_proceeds: Money | None

    # --- outcome -----------------------------------------------------------
    #: Signed: negative is a loss.
    capital_gain: Money | None
    #: Each non-negative, at most one non-zero. The schedule reports them separately.
    gross_gain: Money | None
    gross_loss: Money | None

    # --- characterisation --------------------------------------------------
    method: str
    discount_percentage: Decimal
    regime: str
    #: TAP, NTAP, mixed, or None where residency has not been declared.
    tap_status: str | None = None
    #: Australian residency status on the day of the event, or None where undeclared.
    residency_status: str | None = None
    #: Whether the gain is excluded from the Australian return. Kept, not filtered out, so
    #: reports can explain it.
    is_disregarded: bool = False
    #: The provision that disregards it.
    disregard_reason: str | None = None
    #: Whether the discount used declared residency or was assumed.
    discount_basis: str = residency_module.BASIS_LEGACY
    #: True if only the instrument's TAP override makes this disposal non-taxable.
    tap_override_suppressed_deeming: bool = False

    # --- the 2027 regime ---------------------------------------------------
    #: Whole disposal, or one half of an s112-155 split.
    slice: str = SLICE_WHOLE
    #: The s102-6 category, which sets the order losses are applied. Set from the cutover only.
    gain_category: str | None = None
    #: The Division 114 factor applied to the cost base, or None if not indexed.
    indexation_factor: Decimal | None = None
    #: Why this row is not final, e.g. a missing valuation or CPI quarter, or an outcome that
    #: needs explaining. Makes the schedule a draft.
    pending_reason: str | None = None
    #: Whether the source statement reconciles; None if there was nothing to check.
    source_reconciles: bool | None = None


#: Reported precision: totals at four places, unit prices at six, matching storage. Rounds
#: away Decimal division residue (e.g. 2019.106200000001099999999996).
TOTAL_PLACES = Decimal('0.0001')
UNIT_PLACES = Decimal('0.000001')


@overload
def _money(value: Money, places: Decimal = ...) -> Money: ...


@overload
def _money(value: None, places: Decimal = ...) -> None: ...


def _money(value: Money | None, places: Decimal = TOTAL_PLACES) -> Money | None:
    """Round a Money to `places`, ROUND_HALF_UP to match `convert_to_decimal_field`."""
    if value is None:
        return None
    return Money(value.amount.quantize(places, rounding=ROUND_HALF_UP), value.currency)


def event_fields() -> list[str]:
    """CGTEvent field names in declaration order, for report columns."""
    return [f.name for f in fields(CGTEvent)]


def _gain_category(instrument: 'Instrument | None', deferred: bool = False) -> str:
    """The s102-6 category a gain falls into.

    Residential only for real property; the Subdivision 26-155 quarantining that category
    needs is not implemented.
    """
    if classification.is_real_property(instrument):
        return CGT_GAIN_RESIDENTIAL
    return CGT_GAIN_DEFERRED_NON_RESIDENTIAL if deferred else CGT_GAIN_NON_RESIDENTIAL


def models_2027_regime(account: 'Account | None') -> bool:
    """Whether this portfolio models the 2027 regime. False for None."""
    return bool(getattr(account, 'model_2027_regime', False))


def _regime_for(event_date: date | None) -> str:
    if event_date is None:
        return REGIME_PRE_CUTOVER
    return REGIME_POST_CUTOVER if event_date >= CGT_CUTOVER_DATE else REGIME_PRE_CUTOVER


def _events_from_allocation(allocation: 'SellAllocation', account: 'Account | None' = None,
                            declared: residency_module.Periods | None = None) -> list[CGTEvent]:
    """Events for one SellAllocation: one row, or two if split at the 2027 cutover.

    Proceeds are the sale's apportioned by quantity; the cost base is the parcel's total.
    """
    parcel = allocation.parcel
    sell = allocation.sell
    buy = parcel.buy

    quantity = allocation.quantity
    share_of_sale = (quantity / sell.quantity) if sell.quantity else Decimal('0')

    # Multiply before dividing, which is how SellAllocation.total_capital_gain does it.
    # Dividing first and multiplying after gives an answer that differs in the last digit,
    # so the two routes disagreed about the same quantity -- the exact class of drift this
    # package exists to remove.
    net_proceeds = _money(
        (sell.proceeds * quantity / sell.quantity) if sell.quantity
        else Money(Decimal('0'), sell.proceeds.currency))
    cost_base = _money(parcel.total_cost_base)
    capital_gain = _money(allocation.total_capital_gain)

    gain_amount = getattr(capital_gain, 'amount', Decimal('0'))
    zero = Money(Decimal('0'), sell.proceeds.currency)
    gross_gain = capital_gain if gain_amount > 0 else zero
    gross_loss = -capital_gain if gain_amount < 0 else zero

    eligible = discount_module.is_discount_eligible(buy.date, sell.date)

    tap_status = tap_module.parcel_tap_status(
        account, sell.instrument, buy.date, sell.date, declared=declared)
    is_disregarded, disregard_reason = tap_module.disregard(
        account, tap_status, sell.date, declared=declared)

    whole = CGTEvent(
        source=SOURCE_DISPOSAL,
        event_date=sell.date,
        fiscal_year=allocation.fiscal_year.name if allocation.fiscal_year else None,
        instrument=sell.instrument.name,
        asset_category=classification.asset_category(sell.instrument),
        asset_category_confirmed=bool(sell.instrument.is_classified),

        sell_allocation_id=allocation.id,
        parcel_id=parcel.id,
        buy_id=buy.id,
        sell_id=sell.id,
        legacy_buy_id=buy.legacy_id,
        legacy_sell_id=sell.legacy_id,

        purchase_date=buy.date,
        sale_date=sell.date,
        quantity=quantity,
        days_held=allocation.days_held,

        unit_buy_price=_money(parcel.adjusted_buy_price, UNIT_PLACES),
        buy_consideration=_money(parcel.adjusted_buy_price * quantity),
        buy_brokerage=_money(parcel.adjusted_unit_brokerage * quantity),
        cost_base_adjustments=_money(
            parcel.total_adjustments * share_of_parcel(parcel, quantity)),
        cost_base=cost_base,

        unit_sale_price=_money(sell.unit_price_converted, UNIT_PLACES),
        gross_proceeds=_money(sell.unit_price_converted * quantity),
        sell_brokerage=_money(sell.total_brokerage_converted * share_of_sale),
        net_proceeds=net_proceeds,

        capital_gain=capital_gain,
        gross_gain=gross_gain,
        gross_loss=gross_loss,

        method=METHOD_DISCOUNT if eligible else METHOD_OTHER,
        discount_percentage=discount_module.discount_percentage(
            buy.date, sell.date, account=account, declared=declared),
        regime=_regime_for(sell.date),
        tap_status=tap_status,
        source_reconciles=None,
        residency_status=residency_module.status_on(account, sell.date, declared=declared),
        is_disregarded=is_disregarded,
        disregard_reason=disregard_reason,
        discount_basis=residency_module.basis(account, declared=declared),
        tap_override_suppressed_deeming=tap_module.override_suppresses_deeming(
            account, sell.instrument, buy.date, sell.date, declared=declared),
    )

    # Two gates, and only one of them is a setting. The account's is read here rather than
    # from a module constant, which is what makes it something a person can actually change.
    if sell.date < CGT_CUTOVER_DATE or not models_2027_regime(account):
        # The legal gate and the rollout gate. The first is permanent: a CGT event before
        # 1 July 2027 is governed by the old regime whatever this application later learns.
        # The second is a safety switch, and it is inert in practice for as long as no
        # disposal can have happened after the cutover.
        return [whole]

    return _apply_cutover(whole, allocation, account=account, declared=declared)


def _apply_cutover(whole: CGTEvent, allocation: 'SellAllocation', account: 'Account | None' = None,
                   declared: residency_module.Periods | None = None) -> list[CGTEvent]:
    """Characterise a disposal on or after 1 July 2027.

    Two rows where s112-155 deems the parcel sold at the cutover: a deferred slice keeping the
    discount and a post-cutover slice indexed instead. One row where the deemed sale does not
    apply, or no cutover valuation exists (marked pending).
    """
    parcel = allocation.parcel
    sell = allocation.sell
    buy = parcel.buy
    quantity = allocation.quantity

    indexation_eligible = indexation_module.is_indexation_eligible(
        account, buy.date, sell.date, declared=declared)

    applies, reason = cutover_module.deemed_sale_applies(
        account, buy.date, sell.date, declared=declared)

    if not applies:
        return [_single_post_cutover_event(
            whole, buy, sell, indexation_eligible, reason,
            adjustments_by_year(parcel, quantity))]

    market_value, _source = parcel.market_value_at(
        cutover_module.DEEMED_SALE_DATE, purpose=cutover_module.PURPOSE_CUTOVER)
    if market_value is None:
        return [replace(
            whole,
            gain_category=_gain_category(sell.instrument),
            pending_reason=cutover_module.PENDING_NO_VALUATION.format(
                instrument=sell.instrument.name,
                day=cutover_module.DEEMED_SALE_DATE.isoformat()),
        )]

    market_value = _money(market_value * share_of_parcel(parcel, quantity))
    later_adjustments = adjustments_by_year(parcel, quantity, since=CGT_CUTOVER_DATE)
    return _split_events(
        whole, buy, sell, market_value, indexation_eligible, later_adjustments)


def adjustments_by_year(parcel: 'Parcel', quantity: Decimal,
                        since: date | None = None) -> list[indexation_module.Adjustment]:
    """`quantity`'s share of the parcel's cost base adjustments, by income year end.

    `since` keeps only the years ending on or after it. Those ending after 30 June 2027
    belong to the asset reacquired at the cutover, not the one deemed sold; one for the year
    ending 30 June 2027 stays with the deemed sale, since s104-107B applies it at the end of
    that year. Picked by the adjustment's year rather than the allocation's
    `activation_date`, which becomes the sale or split date whenever an allocation is split.
    Amounts are unrounded; round their total.
    """
    allocations = parcel.cost_base_adjustment_allocation.filter(deactivation_date__isnull=True)
    if since is not None:
        allocations = allocations.filter(
            cost_base_adjustment__financial_year_end_date__gte=since)
    totals = (
        allocations.values('cost_base_adjustment__financial_year_end_date')
        .annotate(total=Sum('cost_base_increase'))
        .order_by('cost_base_adjustment__financial_year_end_date')
    )
    share = share_of_parcel(parcel, quantity)
    # The same currency as Parcel.total_adjustments, which these are a part of.
    currency = parcel.buy.account.currency
    return [
        (row['cost_base_adjustment__financial_year_end_date'],
         Money(row['total'] * share, currency))
        for row in totals if row['total']
    ]


def _outcome(proceeds: Money, indexed_cost_base: Money, plain_cost_base: Money) -> tuple[Money, Money]:
    """Return `(signed gain, cost_base_used)` given indexed and unindexed cost bases.

    Indexation can reduce a gain but never create a loss: a loss uses the reduced cost base,
    which excludes indexation (s110-55). Proceeds between the two give neither.
    """
    zero = _zero_like(proceeds)
    if proceeds > indexed_cost_base:
        return proceeds - indexed_cost_base, indexed_cost_base
    if proceeds < plain_cost_base:
        return proceeds - plain_cost_base, plain_cost_base
    return zero, proceeds


def _single_post_cutover_event(whole: CGTEvent, buy: 'Buy', sell: 'Sell', indexation_eligible: bool,
                               reason: str | None,
                               adjustments: list[indexation_module.Adjustment]) -> CGTEvent:
    """A post-cutover disposal that s112-155 does not split.

    If indexation is available it is mandatory (s110-36(1A)) and removes the discount
    (s115-20). For a returned expatriate denied the split by s112-155(1)(d), that means no
    discount and indexation only from 2027, so the row carries `reason` to explain it.

    Each of `adjustments` is indexed from its own quarter (`indexed_cost_base`). The factor
    reported is the one for the rest of the cost base.
    """
    category = _gain_category(sell.instrument)
    if not indexation_eligible:
        return replace(whole, gain_category=category, pending_reason=reason)

    net_proceeds, plain_cost_base = whole.net_proceeds, whole.cost_base
    assert net_proceeds is not None and plain_cost_base is not None  # set on every disposal
    try:
        factor = indexation_module.indexation_factor(buy.date, sell.date)
        indexed_cost_base = _money(cast(Money, indexation_module.indexed_cost_base(
            plain_cost_base, buy.date, sell.date, adjustments=adjustments)))
    except indexation_module.IndexationDataUnavailable as exc:
        return replace(whole, gain_category=category, pending_reason=str(exc))

    if reason == cutover_module.PENDING_S115_105:
        # That message describes the other outcome, an apportioned discount unindexed.
        reason = cutover_module.PENDING_S115_105_INDEXED

    gain, cost_base_used = _outcome(net_proceeds, indexed_cost_base, plain_cost_base)
    return replace(
        whole,
        cost_base=cost_base_used,
        capital_gain=gain,
        gross_gain=gain if gain.amount > 0 else _zero_like(gain),
        gross_loss=-gain if gain.amount < 0 else _zero_like(gain),
        # s115-20: a gain worked out on an indexed cost base gets no discount at all.
        method=METHOD_OTHER,
        discount_percentage=Decimal('0'),
        indexation_factor=factor,
        gain_category=category,
        pending_reason=reason,
    )


def _split_events(whole: CGTEvent, buy: 'Buy', sell: 'Sell', market_value: Money,
                  indexation_eligible: bool,
                  later: list[indexation_module.Adjustment]) -> list[CGTEvent]:
    """The deferred and post-cutover gains from an s112-155 deemed sale at `market_value`.

    Before indexation they sum to the whole gain. The deferred slice keeps the discount; the
    post-cutover slice is indexed and gets no discount.

    `later` are the cost base adjustments for years after the cutover. They move from the
    deferred slice's cost base to the post-cutover one. The market value is indexed from
    1 July 2027 and each adjustment from its own quarter (`indexed_cost_base`). The factor
    reported is the market value's.
    """
    net_proceeds, plain_cost_base = whole.net_proceeds, whole.cost_base
    whole_adjustments = whole.cost_base_adjustments
    assert (net_proceeds is not None and plain_cost_base is not None
            and whole_adjustments is not None)  # set on every disposal
    later_adjustments = _zero_like(market_value)
    for _year_end, amount in later:
        later_adjustments = later_adjustments + amount
    later_adjustments = _money(later_adjustments)
    deferred_cost_base = plain_cost_base - later_adjustments
    deferred_gain = _money(market_value - deferred_cost_base)

    reacquisition_cost = market_value + later_adjustments
    factor: Decimal | None = Decimal('1.000')
    pending: str | None = None
    indexed_reacquisition_cost = reacquisition_cost
    if indexation_eligible:
        try:
            factor = indexation_module.indexation_factor(CGT_CUTOVER_DATE, sell.date)
            indexed_reacquisition_cost = _money(cast(Money, indexation_module.indexed_cost_base(
                reacquisition_cost, CGT_CUTOVER_DATE, sell.date, adjustments=later)))
        except indexation_module.IndexationDataUnavailable as exc:
            factor = None
            pending = str(exc)

    post_gain, post_cost_base = _outcome(
        net_proceeds, indexed_reacquisition_cost, reacquisition_cost)
    post_gain = _money(post_gain)

    # s114-10(9): the deemed reacquisition is disregarded for the 12-month rule, so the
    # deferred slice is measured from the original purchase and both slices are held for
    # more than twelve months if the parcel was.
    deferred_eligible = discount_module.is_discount_eligible(buy.date, sell.date)

    deferred = replace(
        whole,
        slice=SLICE_PRE_CUTOVER,
        sale_date=cutover_module.DEEMED_SALE_DATE,
        net_proceeds=market_value,
        gross_proceeds=market_value,
        sell_brokerage=_zero_like(market_value),
        unit_sale_price=None,
        cost_base_adjustments=whole_adjustments - later_adjustments,
        cost_base=deferred_cost_base,
        capital_gain=deferred_gain,
        gross_gain=deferred_gain if deferred_gain.amount > 0 else _zero_like(deferred_gain),
        gross_loss=-deferred_gain if deferred_gain.amount < 0 else _zero_like(deferred_gain),
        method=METHOD_DISCOUNT if deferred_eligible else METHOD_OTHER,
        gain_category=_gain_category(sell.instrument, deferred=True),
        indexation_factor=NO_INDEXATION,
    )

    post = replace(
        whole,
        slice=SLICE_POST_CUTOVER,
        purchase_date=CGT_CUTOVER_DATE,
        days_held=(sell.date - CGT_CUTOVER_DATE).days,
        unit_buy_price=None,
        buy_consideration=market_value,
        buy_brokerage=_zero_like(market_value),
        cost_base_adjustments=later_adjustments,
        cost_base=post_cost_base,
        capital_gain=post_gain,
        gross_gain=post_gain if post_gain.amount > 0 else _zero_like(post_gain),
        gross_loss=-post_gain if post_gain.amount < 0 else _zero_like(post_gain),
        # The reacquisition is on the cutover, so the asset is a post-cutover asset and
        # s115-100(aa) and (f) leave it no discount whether or not indexation was available.
        method=METHOD_OTHER,
        discount_percentage=Decimal('0'),
        indexation_factor=factor,
        gain_category=_gain_category(sell.instrument),
        pending_reason=pending,
    )

    return [deferred, post]


def _zero_like(money: Money) -> Money:
    return Money(Decimal('0'), money.currency)


def share_of_parcel(parcel: 'Parcel', quantity: Decimal) -> Decimal:
    """`quantity` as a fraction of the parcel's quantity; zero for an empty parcel."""
    parcel_quantity = parcel.parcel_quantity
    if not parcel_quantity:
        return Decimal('0')
    return quantity / parcel_quantity


def _attribution_events(statement: 'AttributionStatement', account: 'Account | None' = None,
                        declared: residency_module.Periods | None = None) -> list[CGTEvent]:
    """Events for one trust statement: up to one discounted and one other-method gain.

    Discounted gains are grossed up (doubled), because the trust reports them halved and the
    member applies their own discount after losses.
    """
    events: list[CGTEvent] = []
    event_date = statement.financial_year_end_date
    instrument = statement.instrument
    fiscal_year = statement.fiscal_year
    zero = Money(Decimal('0'), instrument.currency)

    common: dict[str, Any] = dict(
        source=SOURCE_ATTRIBUTION,
        event_date=event_date,
        fiscal_year=fiscal_year.name if fiscal_year else None,
        instrument=instrument.name,
        asset_category=classification.asset_category(instrument),
        asset_category_confirmed=bool(instrument.is_classified),
        sell_allocation_id=None,
        parcel_id=None,
        buy_id=None,
        sell_id=None,
        legacy_buy_id=None,
        legacy_sell_id=statement.legacy_id,
        # A trust's own holding period governs its gain, and the member never held the
        # underlying asset, so none of these mean anything here.
        purchase_date=None,
        sale_date=None,
        quantity=None,
        days_held=None,
        unit_buy_price=None,
        buy_consideration=None,
        buy_brokerage=None,
        cost_base_adjustments=None,
        cost_base=None,
        unit_sale_price=None,
        gross_proceeds=None,
        sell_brokerage=None,
        net_proceeds=None,
        regime=_regime_for(event_date),
        source_reconciles=statement.reconciles,
        residency_status=residency_module.status_on(account, event_date, declared=declared),
        discount_basis=residency_module.basis(account, declared=declared),
    )

    def attributed(amount: Money, status: str, method: str, discount_percentage: Decimal) -> None:
        disregarded, reason = tap_module.disregard_attribution(
            account, status, event_date, declared=declared,
            mit_withheld=statement.gain_subject_to_mit_withholding)
        events.append(CGTEvent(
            **common,
            capital_gain=amount,
            gross_gain=amount,
            gross_loss=zero,
            method=method,
            discount_percentage=discount_percentage,
            tap_status=status,
            is_disregarded=disregarded,
            disregard_reason=reason,
        ))

    # The TAP and NTAP parts are separate events, because the statement states each and a
    # foreign resident member disregards only the NTAP part (s855-40(2)). As one event, the
    # NTAP part of a mixed statement was taxed with the TAP part.
    for status, component in ((TAP, 'DISCOUNTED_TAP'), (NTAP, 'DISCOUNTED_NTAP')):
        discounted = statement.component_total(component)
        if discounted:
            attributed(
                _money(Money(discounted * 2, instrument.currency)), status, METHOD_DISCOUNT,
                # The member applies their own discount percentage, not the trust's. For a
                # foreign resident member that is an apportioned one, and the trust has no
                # way of knowing it -- which is exactly why the statement's figure is grossed
                # up first rather than carried through.
                _attributed_discount_percentage(account, declared))

    for status, component in ((TAP, 'OTHER_TAP'), (NTAP, 'OTHER_NTAP')):
        other = statement.component_total(component)
        if other:
            attributed(
                _money(Money(other, instrument.currency)), status, METHOD_OTHER,
                Decimal('0'))

    return events


def _attributed_discount_percentage(account: 'Account | None', declared: residency_module.Periods | None) -> Decimal:
    """The taxpayer type's flat discount rate, not apportioned for residency.

    s115-105 apportions over the trust's ownership period, which the statement does not
    disclose. This only matters for a foreign resident's TAP gains; NTAP ones are
    disregarded (s855-40(2)).
    """
    return discount_module.base_rate(account)


def attribution_events(account: 'Account', fiscal_year: 'FiscalYear | str | None' = None) -> list[CGTEvent]:
    """Capital gains attributed to this account by managed investment trusts."""
    from share_dinkum_app.models import AttributionStatement

    wanted = getattr(fiscal_year, 'name', fiscal_year)

    declared = residency_module.periods(account)

    events: list[CGTEvent] = []
    statements = (
        AttributionStatement.objects.filter(account=account, is_active=True)
        .select_related('instrument', 'instrument__market')
        .order_by('financial_year_end_date', 'id')
    )
    for statement in statements:
        for event in _attribution_events(statement, account=account, declared=declared):
            if wanted is not None and event.fiscal_year != wanted:
                continue
            events.append(event)
    return events


def all_events(account: 'Account', fiscal_year: 'FiscalYear | str | None' = None) -> list[CGTEvent]:
    """Every capital gains event for an account, from whatever source, oldest first."""
    events = disposal_events(account, fiscal_year=fiscal_year)
    events += attribution_events(account, fiscal_year=fiscal_year)
    return sorted(events, key=lambda event: (event.event_date, event.source, event.instrument))


def disposal_events(account: 'Account', fiscal_year: 'FiscalYear | str | None' = None) -> list[CGTEvent]:
    """Every disposal event for an account, oldest sale first.

    `fiscal_year` (a FiscalYear or its name) narrows the result to that year.
    """
    from share_dinkum_app.models import Sell

    wanted = getattr(fiscal_year, 'name', fiscal_year)

    declared = residency_module.periods(account)

    events: list[CGTEvent] = []
    sells = (
        Sell.objects.filter(account=account, is_active=True)
        .select_related('instrument')
        .order_by('date', 'id')
    )
    for sell in sells:
        allocations = (
            sell.sale_allocation.filter(is_active=True)
            .select_related('parcel', 'parcel__buy', 'sell', 'calculated_fiscal_year')
        )
        for allocation in allocations:
            for event in _events_from_allocation(
                    allocation, account=account, declared=declared):
                if wanted is not None and event.fiscal_year != wanted:
                    continue
                events.append(event)
    return events
