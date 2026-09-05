"""CGT events: one row per thing that can produce a capital gain or loss.

A single flat record, built once, that every capital gains report is then a projection of.
The alternative -- each report walking the object graph and recomputing -- is how the two
existing reports came to derive proceeds differently from one another, and how a column
list came to be asserted against dict ordering at runtime.

Today the only source is a disposal: a SellAllocation, which is one parcel being consumed
by one sale. Two further sources are known to be needed and are not built yet. Capital
gains *attributed* by a managed investment trust are a source the application cannot
currently represent at all, and for an ETF-heavy portfolio they are frequently the larger
number. From 1 July 2027 a disposal of a parcel held across that date produces two rows
rather than one, under s112-155. The `source` field exists to keep those distinguishable
when they arrive.
"""

from dataclasses import dataclass, fields, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

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

#: A disposal of a parcel: one SellAllocation.
SOURCE_DISPOSAL = 'disposal'

#: A capital gain attributed by a managed investment trust, from its annual statement.
SOURCE_ATTRIBUTION = 'trust_attribution'

#: Whether the asset the gain arose on was taxable Australian property. For an attribution
#: the trust states it. For a disposal it is derived from the holder's residency history,
#: and is None where none has been declared. Defined in cgt.tap, re-exported here so that a
#: report reading events does not need to know which module owns the vocabulary.
TAP = tap_module.TAP
NTAP = tap_module.NTAP
TAP_MIXED = tap_module.TAP_MIXED

#: Working out the gain by reference to the discount, rather than by indexing the cost
#: base. Until 1 July 2027 it is the only method available to an individual.
METHOD_DISCOUNT = 'discount'

#: No discount, because the holding period test is not met.
METHOD_OTHER = 'other'

REGIME_PRE_CUTOVER = 'pre_2027'
REGIME_POST_CUTOVER = 'post_2027'

#: A disposal that falls wholly on one side of 1 July 2027, or one the deemed sale does not
#: reach. One row, as every disposal has been until now.
SLICE_WHOLE = 'whole'

#: The two halves of a disposal split by s112-155. They share a sell allocation id, so a
#: report can present them as one sale, and they sum to the gain the whole disposal made.
SLICE_PRE_CUTOVER = 'pre_cutover'
SLICE_POST_CUTOVER = 'post_cutover'

#: Indexation was denied to this row, or could not be worked out. Distinct from a factor of
#: 1.000, which means indexation applied and inflation happened to be flat.
NO_INDEXATION = None


@dataclass(frozen=True)
class CGTEvent:
    """One capital gains event, with everything needed to characterise and trace it.

    Frozen because a report should not be able to adjust a figure on its way to a
    spreadsheet; if a number is wrong, it is wrong where it is derived.
    """

    # --- what and when -----------------------------------------------------
    source: str
    event_date: date
    fiscal_year: str | None
    instrument: str
    #: Which box on the CGT schedule this belongs in, derived from the instrument's legal
    #: form and market. 'Unclassified' where the user has not said what the asset is.
    asset_category: str
    #: False where the category rests on a default or a suggestion rather than on the
    #: user's own answer, so a schedule built on it can be marked as a draft.
    asset_category_confirmed: bool

    # --- traceability, so any figure can be followed back to its records ----
    #: The primary keys themselves, not text. Anything needing a string form (the snapshot
    #: payload, a spreadsheet cell) converts at its own boundary, so nothing here changes
    #: the type a report has always emitted.
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
    #: Signed, as the existing reports present it.
    capital_gain: Money | None
    #: Gross gain and gross loss, each non-negative, exactly one of them non-zero. The CGT
    #: schedule asks for these separately per asset category, and netting them inside a
    #: holding loses information the form needs.
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
    #: Whether the gain drops out of the Australian return entirely. A disregarded event is
    #: kept rather than filtered away: it still has to be explainable, and a schedule that
    #: silently omits a large sale is indistinguishable from one that lost it.
    is_disregarded: bool = False
    #: The provision doing the disregarding, for the report to cite.
    disregard_reason: str | None = None
    #: How the discount was arrived at: by declared residency, or by assuming it.
    discount_basis: str = residency_module.BASIS_LEGACY
    #: True where an instrument-level override, and nothing else, is what makes this
    #: disposal non-taxable. The gain is disregarded because of a setting rather than
    #: because of the facts, and the schedule says so.
    tap_override_suppressed_deeming: bool = False

    # --- the 2027 regime ---------------------------------------------------
    #: Whether this row is a whole disposal or one half of one split by s112-155.
    slice: str = SLICE_WHOLE
    #: The s102-6 category, which decides where the gain is reported and, more importantly,
    #: the order in which capital losses are spent against it. Only set from the cutover:
    #: before it, the categories did not exist.
    gain_category: str | None = None
    #: The Division 114 factor applied to the cost base, or None where indexation was not
    #: available. 1.000 is a real answer and means the index did not move.
    indexation_factor: Decimal | None = None
    #: Why this row cannot yet be reported as final: a missing valuation, a missing CPI
    #: quarter, an unclassified asset, or a statutory outcome surprising enough to need
    #: saying out loud. A schedule with any of these is a draft.
    pending_reason: str | None = None
    #: False where the source figures failed their own internal check, None where there was
    #: nothing to check against. A statement that does not reconcile should not be relied on.
    source_reconciles: bool | None = None


#: Money is stored throughout this application at four decimal places, and per-unit prices
#: at six. A figure derived by division carries whatever Decimal's 28 significant digits
#: leave behind -- a gain of exactly $3,179.90 arrives as -3179.90000000000000000000000,
#: and a sum of fourteen of them as 2019.106200000001099999999996. Those trailing digits are
#: not precision, they are the residue of an inexact division, and carrying them into a
#: report makes a dollar amount look like a measurement.
#:
#: Rounding happens here, at the boundary where a computed figure becomes a reported one,
#: and never in the snapshot serialiser. That distinction matters: a snapshot rounded on its
#: way to storage would stop matching a fresh calculation, and the basis change report would
#: call every row changed. Rounding on both sides of that comparison keeps them equal.
TOTAL_PLACES = Decimal('0.0001')
UNIT_PLACES = Decimal('0.000001')


def _money(value, places=TOTAL_PLACES):
    """A money amount at the precision this application actually stores.

    ROUND_HALF_UP, matching `convert_to_decimal_field`, so a figure computed here and the
    same figure written to a column agree rather than differing by a cent at the halfway
    point.
    """
    if value is None:
        return None
    return Money(value.amount.quantize(places, rounding=ROUND_HALF_UP), value.currency)


def event_fields():
    """Field names in declaration order.

    Reports derive their column list from this rather than repeating it, which is what
    removes the need to assert a dict's key order at runtime.
    """
    return [f.name for f in fields(CGTEvent)]


def _gain_category(instrument, deferred=False):
    """The s102-6 category a gain falls into.

    A share portfolio only ever produces non-residential gains, so this is nearly always the
    same answer. The residential branch exists to be honest about a gap rather than to work:
    the residential categories bring in the Subdivision 26-155 quarantining that steps 3 and
    4 of the s102-5 method statement apply, and none of that is implemented.
    """
    if classification.is_real_property(instrument):
        return CGT_GAIN_RESIDENTIAL
    return CGT_GAIN_DEFERRED_NON_RESIDENTIAL if deferred else CGT_GAIN_NON_RESIDENTIAL


def _regime_for(event_date):
    if event_date is None:
        return REGIME_PRE_CUTOVER
    return REGIME_POST_CUTOVER if event_date >= CGT_CUTOVER_DATE else REGIME_PRE_CUTOVER


def _events_from_allocation(allocation, account=None, declared=None):
    """Build one event from a SellAllocation, preserving the app's existing arithmetic.

    Proceeds are apportioned from the sale by quantity, and the cost base is the parcel's
    own total, both exactly as RealisedCapitalGainReport has always computed them.
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

    # Read from the module rather than bound at import: the second of these is a switch,
    # and a switch nothing can flip is not a switch.
    if sell.date < CGT_CUTOVER_DATE or not constants.CGT_2027_REGIME_ENABLED:
        # The legal gate and the rollout gate. The first is permanent: a CGT event before
        # 1 July 2027 is governed by the old regime whatever this application later learns.
        # The second is a safety switch, and it is inert in practice for as long as no
        # disposal can have happened after the cutover.
        return [whole]

    return _apply_cutover(whole, allocation, account=account, declared=declared)


def _apply_cutover(whole, allocation, account=None, declared=None):
    """Characterise a disposal that happens on or after 1 July 2027.

    Returns one row or two. Two where s112-155 deems the parcel sold at the cutover, which
    splits the gain into a deferred half carrying the old 50% discount and a post-cutover
    half carrying indexation instead. One where the deemed sale does not apply, which is
    either because the parcel was bought after the cutover anyway, or because s112-155(1)(d)
    denied it.
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
            whole, buy, sell, indexation_eligible, reason)]

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
    return _split_events(
        whole, buy, sell, market_value, indexation_eligible)


def _outcome(proceeds, indexed_cost_base, plain_cost_base):
    """Gain, loss, or neither, given an indexed and an unindexed cost base.

    Indexation may increase a gain's cost base but must never create or deepen a loss.
    s100-45 and s104-10(4) work a capital loss out against the **reduced cost base**, and
    s110-55 excludes indexation from it. Left unguarded, inflation would manufacture a
    deductible loss out of an asset that merely failed to keep pace with it.

    That leaves three outcomes rather than two, and the middle one is easy to miss: where
    the proceeds land between the two cost bases there is no gain *and* no loss. Reporting
    the indexed figure there would invent a loss; reporting the plain one would invent a
    gain.

    Returns `(gain, cost_base_used)`, the gain signed as the rest of the package expects.
    """
    zero = _zero_like(proceeds)
    if proceeds > indexed_cost_base:
        return proceeds - indexed_cost_base, indexed_cost_base
    if proceeds < plain_cost_base:
        return proceeds - plain_cost_base, plain_cost_base
    return zero, proceeds


def _single_post_cutover_event(whole, buy, sell, indexation_eligible, reason):
    """A post-cutover disposal that s112-155 does not split.

    The interesting case is the returned expatriate. s112-155(1)(d) denies them the deemed
    sale because s115-105 applies to them, so nothing of their pre-2027 growth is banked at
    50%. If they are then an Australian resident from the cutover, s114-25 is satisfied and
    indexation is mandatory under s110-36(1A) -- and s115-20 denies the discount to any gain
    worked out on an indexed cost base. So they lose the discount they would have had and
    get indexation running only from 2027 in its place. That is the Act working as written,
    but it looks so much like a bug that the row carries an explanation.
    """
    category = _gain_category(sell.instrument)
    if not indexation_eligible:
        return replace(whole, gain_category=category, pending_reason=reason)

    try:
        factor = indexation_module.indexation_factor(buy.date, sell.date)
    except indexation_module.IndexationDataUnavailable as exc:
        return replace(whole, gain_category=category, pending_reason=str(exc))

    gain, cost_base_used = _outcome(
        whole.net_proceeds, _money(whole.cost_base * factor), whole.cost_base)
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


def _split_events(whole, buy, sell, market_value, indexation_eligible):
    """The two gains a deemed sale produces.

    Before indexation they sum exactly to the gain the disposal made: the market value is
    subtracted on one side and added back on the other, so it moves gain between the two
    categories without changing the total. Indexation then lifts the reacquisition cost on
    the post-cutover side, and that difference is the relief.

    Which side a dollar lands on still matters, because the two are taxed differently: the
    deferred side keeps the 50% discount, the other gets indexation instead, and losses are
    spent against the deferred side first.
    """
    deferred_gain = _money(market_value - whole.cost_base)

    factor = Decimal('1.000')
    pending = None
    indexed_reacquisition_cost = market_value
    if indexation_eligible:
        try:
            factor = indexation_module.indexation_factor(CGT_CUTOVER_DATE, sell.date)
            indexed_reacquisition_cost = _money(market_value * factor)
        except indexation_module.IndexationDataUnavailable as exc:
            factor = None
            pending = str(exc)

    post_gain, post_cost_base = _outcome(
        whole.net_proceeds, indexed_reacquisition_cost, market_value)
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
        cost_base_adjustments=_zero_like(market_value),
        cost_base=post_cost_base,
        capital_gain=post_gain,
        gross_gain=post_gain if post_gain.amount > 0 else _zero_like(post_gain),
        gross_loss=-post_gain if post_gain.amount < 0 else _zero_like(post_gain),
        # The reacquisition is on the cutover, so the asset is a post-cutover asset and
        # s115-100 leaves it no discount whether or not indexation was available.
        method=METHOD_OTHER,
        discount_percentage=Decimal('0'),
        indexation_factor=factor,
        gain_category=_gain_category(sell.instrument),
        pending_reason=pending,
    )

    return [deferred, post]


def _zero_like(money):
    return Money(Decimal('0'), money.currency)


def share_of_parcel(parcel, quantity):
    """What fraction of a parcel an allocation represents.

    A cost base adjustment is held against the whole parcel, so an allocation that consumes
    part of one carries a proportionate share of it.
    """
    parcel_quantity = parcel.parcel_quantity
    if not parcel_quantity:
        return Decimal('0')
    return quantity / parcel_quantity


def _attribution_events(statement, account=None, declared=None):
    """Events for one annual trust statement.

    Up to two, because the discounted and other-method gains are taxed differently and the
    schedule reports them separately. Netting them into one row would lose the distinction
    and understate the discount available.

    The discounted amount is **grossed up**. A trust reports its discounted gains already
    halved; the member adds the halved part back, applies their own capital losses, and
    then applies their own discount percentage, which need not be the trust's. Carrying the
    trust's halved figure would apply the discount twice.
    """
    events = []
    event_date = statement.financial_year_end_date
    instrument = statement.instrument
    fiscal_year = statement.fiscal_year
    zero = Money(Decimal('0'), instrument.currency)

    common = dict(
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

    discounted_tap = statement.component_total('DISCOUNTED_TAP')
    discounted_ntap = statement.component_total('DISCOUNTED_NTAP')
    discounted = discounted_tap + discounted_ntap
    if discounted:
        grossed_up = _money(Money(discounted * 2, instrument.currency))
        status = _tap_status(discounted_tap, discounted_ntap)
        disregarded, reason = tap_module.disregard_attribution(
            account, status, event_date, declared=declared)
        events.append(CGTEvent(
            **common,
            capital_gain=grossed_up,
            gross_gain=grossed_up,
            gross_loss=zero,
            method=METHOD_DISCOUNT,
            # The member applies their own discount percentage, not the trust's. For a
            # foreign resident member that is an apportioned one, and the trust has no way
            # of knowing it -- which is exactly why the statement's figure is grossed up
            # first rather than carried through.
            discount_percentage=_attributed_discount_percentage(account, declared),
            tap_status=status,
            is_disregarded=disregarded,
            disregard_reason=reason,
        ))

    other_tap = statement.component_total('OTHER_TAP')
    other_ntap = statement.component_total('OTHER_NTAP')
    other = other_tap + other_ntap
    if other:
        amount = _money(Money(other, instrument.currency))
        status = _tap_status(other_tap, other_ntap)
        disregarded, reason = tap_module.disregard_attribution(
            account, status, event_date, declared=declared)
        events.append(CGTEvent(
            **common,
            capital_gain=amount,
            gross_gain=amount,
            gross_loss=zero,
            method=METHOD_OTHER,
            discount_percentage=Decimal('0'),
            tap_status=status,
            is_disregarded=disregarded,
            disregard_reason=reason,
        ))

    return events


def _attributed_discount_percentage(account, declared):
    """The discount a member applies to a gain a trust attributed to them.

    The flat rate for the taxpayer type, without residency apportionment, and the reason is
    a gap in the source document rather than a shortcut. s115-105 apportions over the
    *discount testing period* -- the days the asset was owned -- and for an attributed gain
    the asset was owned by the trust, not by the member. An annual tax statement reports a
    total; it does not disclose when the trust bought what it sold, so the fraction cannot
    be worked out from anything the application holds.

    The exposure this leaves is narrow. A foreign resident's attributed gains on non-TAP
    assets are disregarded outright under s855-40(2), so nothing is apportioned there. What
    remains is a foreign resident's attributed gains on taxable Australian property, where
    the full rate is applied and the schedule flags the assumption.
    """
    return discount_module.base_rate(account)


def _tap_status(tap_amount, ntap_amount):
    """Whether the trust's own asset was taxable Australian property.

    Kept because it decides whether a foreign resident member can disregard the gain --
    a distinction that cannot be recovered once the two are added together.
    """
    if tap_amount and ntap_amount:
        return TAP_MIXED
    if tap_amount:
        return TAP
    if ntap_amount:
        return NTAP
    return None


def attribution_events(account, fiscal_year=None):
    """Capital gains attributed to this account by managed investment trusts."""
    from share_dinkum_app.models import AttributionStatement

    wanted = getattr(fiscal_year, 'name', fiscal_year)

    declared = residency_module.periods(account)

    events = []
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


def all_events(account, fiscal_year=None):
    """Every capital gains event for an account, from whatever source, oldest first."""
    events = disposal_events(account, fiscal_year=fiscal_year)
    events += attribution_events(account, fiscal_year=fiscal_year)
    return sorted(events, key=lambda event: (event.event_date, event.source, event.instrument))


def disposal_events(account, fiscal_year=None):
    """Every disposal for an account, oldest sale first.

    `fiscal_year` accepts a FiscalYear or its name, and narrows the result to that year.
    """
    from share_dinkum_app.models import Sell

    wanted = getattr(fiscal_year, 'name', fiscal_year)

    declared = residency_module.periods(account)

    events = []
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
