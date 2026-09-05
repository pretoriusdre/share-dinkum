"""Taxable Australian property, and the gains a foreign resident can disregard.

A foreign resident is taxed in Australia only on taxable Australian property. s855-10(1)
disregards a capital gain or loss "from a CGT event if ... you are a foreign resident ...
and the CGT event happens in relation to a CGT asset that is not taxable Australian
property". s768-915 extends the same treatment to temporary residents, and s855-40(2) --
with s276-55 for attribution managed investment trusts -- does the equivalent for gains a
trust attributes to a foreign resident member.

For an ordinary holder of listed shares and ETF units, almost nothing is taxable Australian
property. That makes this module mostly a machine for saying "disregarded", and it is worth
being clear why that is not a bug: a foreign resident who sells listed Australian shares
generally has no Australian capital gain at all, and a portfolio tracker that reports one
is overstating their tax by the whole amount.

**TAP is not a property of the instrument.** The tempting implementation -- a list of
tickers that are taxable Australian property -- is wrong in a way that is easy to miss. The
main route by which ordinary listed shares become TAP is the s104-165(3) deeming, which
attaches to the *parcel*: it catches assets owned at the moment the holder left Australia
and leaves later purchases of the same ticker alone. A per-instrument list applies the
answer for the still-open holdings to everything ever sold.
"""

from share_dinkum_app.cgt import classification, residency

TAP = 'TAP'
NTAP = 'NTAP'
TAP_MIXED = 'mixed'

#: Residency has not been declared, so whether the asset is within the Australian net
#: cannot be answered. Never treated as either answer.
TAP_UNKNOWN = None

REASON_FOREIGN_RESIDENT = 's855-10: foreign resident, asset is not taxable Australian property'
REASON_TEMPORARY_RESIDENT = 's768-915: temporary resident, asset is not taxable Australian property'
REASON_TRUST_ATTRIBUTION = 's855-40(2) and s276-55: foreign resident member, gain not attributable to taxable Australian property'


def instrument_tap_status(instrument):
    """The instrument's own contribution, from the s855-15 table items 1 to 4.

    Returns None where the instrument says nothing, which is the normal case: listed shares
    and units are not taxable Australian property on their own account, and become so only
    through the parcel-level deeming below.

    `Instrument.is_taxable_australian_property_override` is a deliberate tri-state. True is
    for the genuine table items -- direct Australian real property, an indirect interest in
    an entity whose value is principally Australian land, an asset used in an Australian
    permanent establishment. False overrules the derivation in the other direction. None,
    the normal case, means derive.

    False is the dangerous one, and the field is named for what it does because of it: it
    is the truthful answer about an ordinary listed share considered on its own, and it
    also suppresses the s104-165(3) deeming below, which is the main route by which such a
    share becomes taxable Australian property. Set across a portfolio it reads every gain
    as disregarded.
    """
    override = getattr(instrument, 'is_taxable_australian_property_override', None)
    if override is not None:
        return TAP if override else NTAP
    if classification.is_real_property(instrument):
        return TAP
    return None


def i1_deeming_applies(account, acquisition_date, event_date, declared=None):
    """Whether s104-165(3) holds this parcel inside the Australian CGT net.

    Leaving Australia triggers CGT event I1 on everything held that is not already taxable
    Australian property. s104-165(2) allows that gain to be disregarded instead, and
    s104-165(3) is the price: each asset held at that moment "is taken to be taxable
    Australian property until the earlier of" a CGT event happening to it, or the holder
    again becoming an Australian resident.

    So the deeming is bounded at both ends. It catches only what was already owned on the
    day of departure -- a purchase made a week later is outside it -- and it lapses on
    return, which means a parcel sold after coming back and leaving again is governed by
    the *second* departure, not the first.
    """
    if acquisition_date is None or event_date is None:
        return False
    if declared is None:
        declared = residency.periods(account)

    for period in declared:
        if period.status == residency.RESIDENT:
            continue
        if not period.i1_election_made:
            continue
        if acquisition_date >= period.start_date:
            # Acquired after the departure, so never owned at the I1 moment.
            continue
        if event_date < period.start_date:
            # Sold before the departure, so it was not owned at the I1 moment either. The
            # deeming reaches what you still held when you left, not everything you ever
            # bought beforehand -- and for a disposal while still resident the question does
            # not arise at all, since a resident is taxed on the gain either way.
            continue
        if period.end_date is not None and event_date > period.end_date:
            # Residency resumed before the sale, so the deeming has already lapsed.
            continue
        return True
    return False


def override_suppresses_deeming(account, instrument, acquisition_date, event_date,
                                declared=None):
    """Whether a "no" on the instrument is the only thing making this disposal NTAP.

    True where the parcel would have been deemed taxable Australian property by
    s104-165(3), and an instrument-level override overrules that. The gain is then
    disregarded on the strength of a setting rather than of the facts, which is worth
    saying out loud: the failure is silent and large. A portfolio-wide "no" takes every
    assessable gain to zero, and zero is a plausible-looking answer for a foreign resident
    holding listed shares -- it is the *right* answer for anyone who did not leave under an
    I1 election, so nothing about the figure itself looks wrong.

    An override that agrees with the derivation is not reported. Only a disagreement is
    worth a warning, or every instrument in an ordinary portfolio would raise one.
    """
    if instrument_tap_status(instrument) != NTAP:
        return False
    return i1_deeming_applies(account, acquisition_date, event_date, declared=declared)


def parcel_tap_status(account, instrument, acquisition_date, event_date, declared=None):
    """Whether a disposal is of taxable Australian property.

    Returns TAP, NTAP, or None where residency has not been declared and the question
    therefore has no answer.
    """
    from_instrument = instrument_tap_status(instrument)
    if from_instrument is not None:
        return from_instrument

    if declared is None:
        declared = residency.periods(account)
    if not declared:
        return TAP_UNKNOWN

    if i1_deeming_applies(account, acquisition_date, event_date, declared=declared):
        return TAP
    return NTAP


def disregard(account, tap_status, event_date, declared=None):
    """Whether a gain is disregarded, and the provision that does it.

    Returns `(is_disregarded, reason)`. A gain is only ever disregarded on an affirmative
    finding: the holder was declared a foreign or temporary resident on the day of the
    event, *and* the asset was determined not to be taxable Australian property. Anything
    unknown stays assessable, because the failure mode of over-reporting a gain is a larger
    tax bill, and the failure mode of under-reporting one is a false return.
    """
    if tap_status != NTAP:
        return False, None
    if declared is None:
        declared = residency.periods(account)
    if not declared:
        return False, None

    status = residency.status_on(account, event_date, declared=declared)
    if status == residency.FOREIGN:
        return True, REASON_FOREIGN_RESIDENT
    if status == residency.TEMPORARY:
        return True, REASON_TEMPORARY_RESIDENT
    return False, None


def disregard_attribution(account, tap_status, event_date, declared=None):
    """The same question for a capital gain attributed by a trust.

    s855-40(2) disregards a foreign resident beneficiary's capital gain to the extent it is
    attributable to a CGT event on an asset that is not taxable Australian property, and
    s276-55 applies that to an attribution managed investment trust member. The trust's own
    statement supplies the split, which is why it is kept rather than netted.

    A statement reporting both -- TAP_MIXED -- is not disregarded here. It could be split in
    proportion, but the statement already states the two amounts separately and the caller
    builds one event from each, so a mixed status reaching this point means the components
    were not separable and guessing at the ratio would be inventing a figure.
    """
    if tap_status != NTAP:
        return False, None
    if declared is None:
        declared = residency.periods(account)
    if not declared:
        return False, None

    status = residency.status_on(account, event_date, declared=declared)
    if status in (residency.FOREIGN, residency.TEMPORARY):
        return True, REASON_TRUST_ATTRIBUTION
    return False, None
