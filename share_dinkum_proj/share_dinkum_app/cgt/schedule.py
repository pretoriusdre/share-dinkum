"""The s102-5 method statement: turning a year's CGT events into one net capital gain.

Three things happen here that a naive sum of gains gets wrong.

**Losses are netted across the year, not floored per parcel.** A loss on one parcel reduces
a gain on another. Reporting each disposal's gain in isolation, never below zero, overstates
the year by the whole of every loss.

**Losses are applied before the discount, never after.** s102-5 Step 1 and Step 2 reduce the
gains; Step 5 then discounts what is left. A $100 gain and a $100 loss net to nothing. Take
the discount first and the same facts produce a $50 gain and a $100 loss, which is a $50 loss
-- a different answer, and a wrong one.

**From 1 July 2027 the order the losses are spent in is prescribed, and it runs against the
taxpayer.** s102-5 Step 1 requires losses to reduce deferred non-residential gains first,
then deferred residential, then non-residential, then residential. The deferred categories
are the ones that kept the 50% discount, so the Act spends losses where they are worth least.
Before the cutover there are no categories and the choice is entirely the taxpayer's, so this
module makes the favourable one: losses go against the least-discounted gains first, where a
dollar of loss saves the most tax.

Discretion survives *within* a category after the cutover (Step 1, Note 3), and is exercised
the same way.

Out of scope, and stated on the schedule rather than left to be discovered: the Subdivision
26-155 quarantining at steps 3 and 4, which only bites on residential gains; the
`minimum tax gap amount` of s119-10(2), which needs the taxpayer's whole taxable income; and
wash sales, rollovers, deceased estates and the small business concessions.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from djmoney.money import Money

from share_dinkum_app.choices import CGTAssetCategory, TaxpayerType
from share_dinkum_app.cgt import discount, events as events_module, residency
from share_dinkum_app.constants import (
    CGT_LOSS_ABSORPTION_ORDER,
    CGT_GAIN_DEFERRED_RESIDENTIAL,
    CGT_GAIN_RESIDENTIAL,
)

#: Where a year has no s102-6 categories because it predates them, everything sits in one
#: pool and the taxpayer chooses the order freely.
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
    #: The base for the Division 119 minimum tax: gains remaining after step 6, before the
    #: Division 30 and 31 deductions this application does not hold.
    minimum_tax_capital_gain_base: Money
    warnings: list = field(default_factory=list)

    @property
    def is_draft(self):
        """Whether any figure here rests on something unconfirmed.

        A draft schedule is not a broken one, but it must not be presented as final. Every
        reason it is a draft is in `warnings`.
        """
        return bool(self.warnings)


def _zero(currency):
    return Money(Decimal('0'), currency)


def _category_of(event):
    """Which pool a gain is netted in.

    Pre-cutover events carry no s102-6 category, because the categories did not exist, so
    they share one pool.
    """
    return event.gain_category or UNCATEGORISED


def _ordered_categories(present):
    """Categories in the order losses are spent against them.

    The statutory order for anything the categories apply to, then anything else. A year
    that straddles the cutover can hold both, which is why this does not simply pick one.
    """
    ordered = [c for c in CGT_LOSS_ABSORPTION_ORDER if c in present]
    ordered += [c for c in present if c not in CGT_LOSS_ABSORPTION_ORDER]
    return ordered


def _spend(pool, gains):
    """Apply a pool of losses across gains, least-discounted first.

    Returns `(applied_by_index, remaining_pool)`. Within a category the taxpayer chooses the
    order, and this is the choice worth making: a dollar of loss taken off a gain that is
    fully taxed saves twice what it saves taken off a half-discounted one.
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
    """Losses from years *before* this one.

    A loss is only available against a later year. Summing every carry-forward row
    regardless of date would apply a loss made in 2026 to a schedule for 2021, which is not
    a small error -- it is a deduction claimed years before it existed.
    """
    from share_dinkum_app.models import CapitalLossCarryForward, FiscalYear

    rows = CapitalLossCarryForward.objects.filter(account=account, is_active=True)

    year = fiscal_year
    if year is not None and not hasattr(year, 'start_year'):
        year = FiscalYear.objects.filter(name=str(year)).first()
    if year is not None:
        rows = rows.filter(fiscal_year__start_year__lt=year.start_year)

    return sum((row.amount for row in rows.select_related('fiscal_year')), zero)


def build(account, fiscal_year, prior_year_losses=None):
    """The s102-5 method statement for one fiscal year.

    `prior_year_losses` overrides what is read from `CapitalLossCarryForward`, which is what
    lets a caller model "what if I had another ten thousand of losses" without writing rows.
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


def _statements_disagreeing_on_cost_base(account, year_name):
    """Statements whose cost base line contradicts the adjustment linked to them.

    Queried from the statements rather than from the events, and that is the whole point.
    An attribution event is only built where a statement attributed a capital gain, so a
    statement declaring nil gains and a large cost base movement -- which is most of them
    for a property trust -- produces no event at all and would be invisible to a check that
    walked the year's events.
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
    """Everything that stops this schedule being final.

    Each of these is a case where the application knows it does not know something. Emitting
    a confident figure over any of them would be the failure mode worth avoiding: an
    incomplete answer that looks complete.

    Most checks read `live_events`, the rows that reach the schedule. One reads `all_events`
    and has to: a disregarded row has already dropped out of `live_events`, so a check that
    asks why something was disregarded cannot be written against what is left. That is the
    same blind spot in miniature -- the rows worth questioning are exactly the ones a
    schedule stops carrying.
    """
    warnings = []

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

    for reason in sorted({e.pending_reason for e in live_events if e.pending_reason}):
        warnings.append(reason)

    residential = {CGT_GAIN_RESIDENTIAL, CGT_GAIN_DEFERRED_RESIDENTIAL}
    if any(e.gain_category in residential for e in live_events):
        warnings.append(
            'This year includes a residential capital gain. The Subdivision 26-155 '
            'quarantining at steps 3 and 4 of the s102-5 method statement is not '
            'implemented, so the figure below is understated.')

    return warnings
