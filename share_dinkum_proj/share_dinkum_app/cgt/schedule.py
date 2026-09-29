"""The s102-5 method statement: a year's CGT events to one net capital gain.

* Current-year then prior-year losses are netted across the year, before the discount.
* From 1 July 2027 losses are applied in the s102-6 category order (s102-5 Step 1).
  Within a category, and before the cutover, they go against the least-discounted gains
  first, which saves the most tax.

Not implemented: Subdivision 26-155 quarantining (residential gains only, warned about),
the s119-10(2) minimum tax gap amount, wash sales, rollovers, deceased estates and small
business concessions.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from djmoney.money import Money

from share_dinkum_app.choices import CGTAssetCategory, TaxpayerType
from share_dinkum_app.cgt import discount, events as events_module, residency
from share_dinkum_app.constants import (
    CGT_LOSS_ABSORPTION_ORDER,
    CGT_GAIN_DEFERRED_RESIDENTIAL,
    CGT_GAIN_RESIDENTIAL,
)

#: The single pool for gains with no s102-6 category (pre-cutover).
UNCATEGORISED = 'capital gain'


@dataclass(frozen=True)
class ScheduleLine:
    """One s102-6 category, from gross gains down to what is actually taxed."""

    category: str
    gross_gains: Money
    current_year_losses_applied: Money
    prior_year_losses_applied: Money
    gain_before_discount: Money
    discount_applied: Money
    net_gain: Money


@dataclass(frozen=True)
class Schedule:
    """A year's capital gains position, and everything qualifying it."""

    fiscal_year: str | None
    basis: str
    lines: list
    gross_gains: Money
    gross_losses: Money
    disregarded_gains: Money
    current_year_losses_applied: Money
    prior_year_losses_applied: Money
    total_discount: Money
    net_capital_gain: Money
    losses_carried_forward: Money
    #: Division 119 minimum tax base: the net gain, before Division 30 and 31 deductions.
    minimum_tax_capital_gain_base: Money
    warnings: list = field(default_factory=list)

    @property
    def is_draft(self):
        """True if there are any `warnings`."""
        return bool(self.warnings)


def _zero(currency):
    return Money(Decimal('0'), currency)


def _category_of(event):
    """The event's s102-6 category, or UNCATEGORISED."""
    return event.gain_category or UNCATEGORISED


def _ordered_categories(present):
    """`present` categories in statutory loss order, then any others."""
    ordered = [c for c in CGT_LOSS_ABSORPTION_ORDER if c in present]
    ordered += [c for c in present if c not in CGT_LOSS_ABSORPTION_ORDER]
    return ordered


def _spend(pool, gains):
    """Apply `pool` to `(amount, discount rate)` gains, lowest rate first.

    Returns `(applied_by_index, remaining_pool)`.
    """
    applied = [Decimal('0')] * len(gains)
    order = sorted(range(len(gains)), key=lambda i: gains[i][1])
    for index in order:
        if pool <= 0:
            break
        amount = min(pool, gains[index][0])
        applied[index] = amount
        pool -= amount
    return applied, pool


def _carried_forward_into(account, fiscal_year, zero):
    """Carried-forward losses still available to `fiscal_year` (every recorded loss if None).

    A recorded loss becomes available the year after it was made, less whatever the years
    in between used. Each of those years is built in turn with the pool as it then stood,
    so a loss applied once is not applied again.
    """
    from share_dinkum_app.models import CapitalLossCarryForward, FiscalYear

    rows = CapitalLossCarryForward.objects.filter(account=account, is_active=True)

    year = fiscal_year
    if year is not None and not hasattr(year, 'start_year'):
        year = FiscalYear.objects.filter(name=str(year)).first()
    if year is None:
        return sum((row.amount for row in rows), zero)

    rows = list(
        rows.filter(fiscal_year__start_year__lt=year.start_year)
        .select_related('fiscal_year')
        .order_by('fiscal_year__start_year'))
    if not rows:
        return zero

    # Only years with a FiscalYear row can hold events, so the rest use nothing.
    years_between = FiscalYear.objects.filter(
        fiscal_year_type=year.fiscal_year_type,
        start_year__gt=rows[0].fiscal_year.start_year,
        start_year__lt=year.start_year,
    ).order_by('start_year')

    pool = zero
    pending = list(rows)
    for between in years_between:
        while pending and pending[0].fiscal_year.start_year < between.start_year:
            pool += pending.pop(0).amount
        pool -= build(account, between, prior_year_losses=pool).prior_year_losses_applied

    for row in pending:
        pool += row.amount
    return pool


def build(account, fiscal_year, prior_year_losses=None):
    """Build the Schedule for one fiscal year.

    `prior_year_losses` overrides the total read from `CapitalLossCarryForward`.
    """
    currency = account.currency
    zero = _zero(currency)

    all_events = events_module.all_events(account, fiscal_year=fiscal_year)
    year_name = getattr(fiscal_year, 'name', fiscal_year)

    # s855-10 disregards a foreign resident's capital loss on non-TAP just as it disregards
    # the gain. Letting a disregarded loss shelter an assessable gain would be claiming a
    # deduction for something Australia never taxed.
    live = [e for e in all_events if not e.is_disregarded]
    disregarded = [e for e in all_events if e.is_disregarded]

    gross_gains = sum((e.gross_gain for e in live if e.gross_gain), zero)
    gross_losses = sum((e.gross_loss for e in live if e.gross_loss), zero)
    disregarded_gains = sum((e.gross_gain for e in disregarded if e.gross_gain), zero)

    # (amount, discount percentage) per gain, grouped by category.
    by_category = {}
    for event in live:
        amount = getattr(event.gross_gain, 'amount', Decimal('0'))
        if amount <= 0:
            continue
        by_category.setdefault(_category_of(event), []).append(
            (amount, event.discount_percentage or Decimal('0')))

    categories = _ordered_categories(list(by_category))

    current_pool = getattr(gross_losses, 'amount', Decimal('0'))
    if prior_year_losses is None:
        prior_year_losses = _carried_forward_into(account, fiscal_year, zero)
    prior_pool = getattr(prior_year_losses, 'amount', Decimal('0'))

    lines = []
    total_net = Decimal('0')
    total_discount = Decimal('0')
    total_current_applied = Decimal('0')
    total_prior_applied = Decimal('0')

    for category in categories:
        gains = by_category[category]

        current_applied, current_pool = _spend(current_pool, gains)
        remaining = [
            (amount - taken, rate)
            for (amount, rate), taken in zip(gains, current_applied)
        ]
        prior_applied, prior_pool = _spend(prior_pool, remaining)

        before_discount = Decimal('0')
        discount = Decimal('0')
        for (amount, rate), taken in zip(remaining, prior_applied):
            left = amount - taken
            before_discount += left
            discount += left * rate

        lines.append(ScheduleLine(
            category=category,
            gross_gains=Money(sum(a for a, _ in gains), currency),
            current_year_losses_applied=Money(sum(current_applied), currency),
            prior_year_losses_applied=Money(sum(prior_applied), currency),
            gain_before_discount=Money(before_discount, currency),
            discount_applied=Money(discount, currency),
            net_gain=Money(before_discount - discount, currency),
        ))

        total_current_applied += sum(current_applied)
        total_prior_applied += sum(prior_applied)
        before = before_discount
        total_discount += discount
        total_net += before - discount

    carried_forward = current_pool + prior_pool

    return Schedule(
        fiscal_year=year_name,
        basis=residency.basis(account),
        lines=lines,
        gross_gains=gross_gains,
        gross_losses=gross_losses,
        disregarded_gains=disregarded_gains,
        current_year_losses_applied=Money(total_current_applied, currency),
        prior_year_losses_applied=Money(total_prior_applied, currency),
        total_discount=Money(total_discount, currency),
        net_capital_gain=Money(total_net, currency),
        losses_carried_forward=Money(carried_forward, currency),
        # s119-5: the gains remaining after step 6. The Division 30 and 31 deductions that
        # reduce it are not portfolio data, so this is the base and not the final figure.
        minimum_tax_capital_gain_base=Money(total_net, currency),
        warnings=_warnings(account, live, all_events, year_name),
    )


def _year_still_running(fiscal_year):
    """The fiscal year's end date if it has not passed yet, else None (and None for None)."""
    from share_dinkum_app.models import FiscalYear

    if fiscal_year is None:
        # The all-years view, which is a position rather than a return, and is provisional
        # for the same reason if it reaches into the current year. Callers that mean a
        # return always name a year, so there is nothing useful to say here.
        return None

    year = fiscal_year
    if not hasattr(year, 'start_year'):
        year = FiscalYear.objects.filter(name=str(year)).first()
    if year is None:
        return None

    end_date = year.end_date
    return end_date if end_date and date.today() <= end_date else None


def _s115_105_applies(account, event):
    """Whether the event's discount is governed by s115-105 (a foreign or temporary holder).

    For a disposal, any such day after 8 May 2012 while it was held. For a trust
    attribution, which has no holding period, the member's status at the year end.
    """
    if event.purchase_date is not None and event.sale_date is not None:
        return residency.has_non_resident_days_after_cutoff(
            account, event.purchase_date, event.sale_date)
    return event.residency_status in (residency.FOREIGN, residency.TEMPORARY)


def _sales_not_fully_allocated(account, year_name):
    """Sales in the year (every year if None) with units allocated to no parcel."""
    from share_dinkum_app.models import Sell

    sales = Sell.with_unallocated_quantity(account)
    if year_name is not None:
        sales = sales.filter(calculated_fiscal_year__name=year_name)
    return list(sales)


def _statements_disagreeing_on_cost_base(account, year_name):
    """Statements in the year whose stated cost base movement disagrees with their linked
    adjustment.

    Queried from statements, not events, because a statement with no capital gain produces
    no event.
    """
    from share_dinkum_app.models import AttributionStatement

    disagreeing = []
    statements = (
        AttributionStatement.objects
        .filter(account=account, is_active=True, cost_base_adjustment__isnull=False)
        .select_related('instrument', 'cost_base_adjustment')
    )
    for statement in statements:
        fiscal_year = statement.fiscal_year
        if year_name is not None and getattr(fiscal_year, 'name', None) != year_name:
            continue
        if statement.cost_base_agrees is False:
            disagreeing.append(statement)
    return disagreeing


def _warnings(account, live_events, all_events, year_name=None):
    """Every reason this schedule is not final, as messages.

    Most checks use `live_events`; the cutover and TAP override checks use `all_events`,
    which includes disregarded rows.
    """
    warnings = []

    # First, because it qualifies everything below it. The other warnings say a figure may
    # be wrong; this one says the year is not over, so the figure is not yet the answer to
    # anything.
    still_running = _year_still_running(year_name)
    if still_running:
        warnings.append(
            f'The {year_name} fiscal year has not ended -- it runs to '
            f'{still_running:%d %B %Y}. These are the figures so far, not the year\'s '
            'figures: anything bought, sold or distributed before then changes them, and '
            'so does the discount on every parcel still being held.')

    # Only where the year actually holds one. A schedule for 2011 is unaffected by this
    # setting either way: `regime` comes from the event date, and no event before
    # 1 July 2027 can be post-cutover.
    after_cutover = sorted({
        event.instrument for event in all_events
        if event.regime == events_module.REGIME_POST_CUTOVER})

    if after_cutover and events_module.models_2027_regime(account):
        warnings.append(
            'This year is modelled under the 2027 capital gains regime, so its figures are '
            'projections rather than settled amounts: CPI has not been published for any '
            'quarter after the cutover, and the method for splitting a straddling gain '
            'without a market valuation has not been made. Untick "Model 2027 regime" on '
            'the portfolio (Accounts in the admin) to go back to the law as it stands. '
            f'Affected: {", ".join(after_cutover)}.')
    elif after_cutover:
        # The direction that loses money quietly. A disposal on or after 1 July 2027 is
        # governed by the new regime whether or not this application models it, so leaving
        # the setting off does not make these figures cautious -- it makes them the old law
        # applied to a year the old law does not reach.
        warnings.append(
            'This year has disposals on or after 1 July 2027, which the new capital gains '
            'regime governs, but they are being worked out under the rules that applied '
            'before it. Tick "Model 2027 regime" on the portfolio (Accounts in the admin) '
            'to model the new law instead; the figures it gives are projections while CPI '
            f'for those quarters is unpublished. Affected: {", ".join(after_cutover)}.')

    if residency.basis(account) == residency.BASIS_LEGACY:
        warnings.append(
            'Residency has not been declared, so every gain here assumes an Australian '
            'resident throughout and a flat 50% discount (s115-105 and s115-115 are not '
            'applied).')
    else:
        warnings.extend(residency.coverage_problems(account))

    if discount.taxpayer_type_of(account) == TaxpayerType.UNDECLARED:
        warnings.append(
            'The account does not say who owns this portfolio, so the discount rate assumes '
            'an individual. A company gets none and a complying superannuation fund a third.')

    unclassified = sorted({
        e.instrument for e in live_events
        if e.asset_category == CGTAssetCategory.UNCLASSIFIED})
    if unclassified:
        warnings.append(
            'These instruments have not been classified, so their gains cannot be placed on '
            f'the schedule: {", ".join(unclassified)}.')

    unconfirmed = sorted({
        e.instrument for e in live_events if not e.asset_category_confirmed})
    if unconfirmed:
        warnings.append(
            'The asset category for these instruments was suggested rather than confirmed: '
            f'{", ".join(unconfirmed)}.')

    overridden = sorted({
        e.instrument for e in all_events if e.tap_override_suppressed_deeming})
    if overridden:
        warnings.append(
            'These instruments are set to "no" for taxable Australian property, and that '
            'setting is the only reason their gains are disregarded: the parcels were held '
            'when Australian residency ceased, so s104-165(3) would otherwise deem them '
            'taxable Australian property. Clear the setting to have it worked out per '
            f'parcel instead: {", ".join(overridden)}.')

    disagreeing = _statements_disagreeing_on_cost_base(account, year_name)
    if disagreeing:
        detail = '; '.join(
            f'{s.instrument.name} states {s.stated_cost_base_movement} '
            f'and the adjustment records {s.cost_base_adjustment.cost_base_increase.amount}'
            for s in sorted(disagreeing, key=lambda s: s.instrument.name))
        warnings.append(
            'These annual statements disagree with the cost base adjustment recorded '
            f'against them, so one of the two was misread: {detail}. A cost base adjustment '
            'is spread across parcels once, when it is created, so a wrong figure here has '
            'already moved every gain derived from those parcels.')

    unreconciled = sorted({
        e.instrument for e in live_events if e.source_reconciles is False})
    if unreconciled:
        warnings.append(
            'These trust statements do not reconcile against themselves, so their attributed '
            f'gains are shown but should not be relied on: {", ".join(unreconciled)}.')

    unsettled = sorted({
        e.instrument for e in live_events
        if e.regime == events_module.REGIME_POST_CUTOVER
        and (e.discount_percentage or 0) > 0
        and _s115_105_applies(account, e)})
    if unsettled:
        warnings.append(
            'These gains, on or after 1 July 2027, keep a discount apportioned for time as a '
            'foreign or temporary resident (s115-105, s115-115). Whether that survives the 2027 '
            'changes is not settled: those sections are unamended, but new s115-100(f) sets 0% '
            'where no other paragraph applies, which would leave no discount at all. Take '
            f'advice before relying on these figures: {", ".join(unsettled)}.')

    unallocated = _sales_not_fully_allocated(account, year_name)
    if unallocated:
        detail = '; '.join(
            f'{s.instrument.name} on {s.date:%d %B %Y} '
            f'({(s.quantity - s.allocated).normalize():f} of {s.quantity.normalize():f} units)'
            for s in unallocated)
        warnings.append(
            'These sales have units not allocated to any parcel, so the gain on those units '
            f'is missing from this schedule: {detail}. The usual causes are a sale larger '
            'than the holding, a sale dated before its purchase, or a MANUAL sale with no '
            'sell allocations entered.')

    for reason in sorted({e.pending_reason for e in live_events if e.pending_reason}):
        warnings.append(reason)

    residential = {CGT_GAIN_RESIDENTIAL, CGT_GAIN_DEFERRED_RESIDENTIAL}
    if any(e.gain_category in residential for e in live_events):
        warnings.append(
            'This year includes a residential capital gain. The Subdivision 26-155 '
            'quarantining at steps 3 and 4 of the s102-5 method statement is not '
            'implemented, so the figure below is understated.')

    return warnings
