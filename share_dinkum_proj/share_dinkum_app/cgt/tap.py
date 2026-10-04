"""Taxable Australian property (TAP), and the gains a foreign resident can disregard.

A foreign or temporary resident's gain on non-TAP is disregarded (s855-10, s768-915), as is
a trust-attributed gain on non-TAP (s855-40(2), s276-55). Listed shares are usually non-TAP.

TAP status is worked out per parcel, not per instrument: the s104-165(3) deeming applies
only to parcels held on departure.
"""

from datetime import date, timedelta
from typing import TYPE_CHECKING

from share_dinkum_app.cgt import classification, residency

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, Instrument

TAP = 'TAP'
NTAP = 'NTAP'

#: Residency is undeclared, so TAP status is unknown. Never treated as TAP or NTAP.
TAP_UNKNOWN = None

REASON_FOREIGN_RESIDENT = 's855-10: foreign resident, asset is not taxable Australian property'
REASON_TEMPORARY_RESIDENT = 's768-915: temporary resident, asset is not taxable Australian property'
REASON_TRUST_ATTRIBUTION = 's855-40(2) and s276-55: foreign resident member, gain not attributable to taxable Australian property'
REASON_MIT_WITHHOLDING = 's840-815: foreign resident member, MIT withholding tax on the gain is final'


def instrument_tap_status(instrument: 'Instrument | None') -> str | None:
    """TAP or NTAP if the instrument decides it alone, else None (the normal case).

    The instrument's override wins if set; otherwise real property is TAP. A "no" override
    also suppresses the parcel-level s104-165(3) deeming.
    """
    override = getattr(instrument, 'is_taxable_australian_property_override', None)
    if override is not None:
        return TAP if override else NTAP
    if classification.is_real_property(instrument):
        return TAP
    return None


def _absences(declared: residency.Periods) -> list[tuple[date, date | None, bool]]:
    """Runs of back-to-back non-resident periods, as `(start, end, election made)`.

    One departure can be recorded as several periods, say FOREIGN then TEMPORARY, or split
    where an election was noted. The election is made once, on leaving, so it covers the
    whole run until residency resumes.
    """
    absences: list[tuple[date, date | None, bool]] = []
    for period in sorted(declared, key=lambda p: p.start_date):
        if period.status == residency.RESIDENT:
            continue
        previous = absences[-1] if absences else None
        if (previous is not None and previous[1] is not None
                and period.start_date == previous[1] + timedelta(days=1)):
            absences[-1] = (previous[0], period.end_date,
                            previous[2] or bool(period.i1_election_made))
        else:
            absences.append(
                (period.start_date, period.end_date, bool(period.i1_election_made)))
    return absences


def i1_deeming_applies(account: 'Account | None', acquisition_date: date | None, event_date: date | None,
                       declared: residency.Periods | None = None) -> bool:
    """Whether s104-165(3) deems this parcel TAP.

    True if some absence with an I1 election began after the acquisition and covers the
    sale: the parcel was held on departure and sold before residency resumed.
    """
    if acquisition_date is None or event_date is None:
        return False
    if declared is None:
        declared = residency.periods(account)

    for start_date, end_date, election_made in _absences(declared):
        if not election_made:
            continue
        if acquisition_date >= start_date:
            # Acquired after the departure, so never owned at the I1 moment.
            continue
        if event_date < start_date:
            # Sold before the departure, so it was not owned at the I1 moment either. The
            # deeming reaches what you still held when you left, not everything you ever
            # bought beforehand -- and for a disposal while still resident the question does
            # not arise at all, since a resident is taxed on the gain either way.
            continue
        if end_date is not None and event_date > end_date:
            # Residency resumed before the sale, so the deeming has already lapsed.
            continue
        return True
    return False


def override_suppresses_deeming(account: 'Account | None', instrument: 'Instrument | None',
                                acquisition_date: date | None, event_date: date | None,
                                declared: residency.Periods | None = None) -> bool:
    """Whether the instrument resolves to NTAP while s104-165(3) would deem the parcel TAP.

    Flags a gain disregarded because of a setting rather than the facts.
    """
    if instrument_tap_status(instrument) != NTAP:
        return False
    return i1_deeming_applies(account, acquisition_date, event_date, declared=declared)


def parcel_tap_status(account: 'Account | None', instrument: 'Instrument | None', acquisition_date: date | None,
                      event_date: date | None, declared: residency.Periods | None = None) -> str | None:
    """TAP or NTAP for a disposal, or None if residency is undeclared."""
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


def disregard(account: 'Account | None', tap_status: str | None, event_date: date | None,
              declared: residency.Periods | None = None) -> tuple[bool, str | None]:
    """Return `(is_disregarded, reason)` for a disposal.

    Disregarded only if the asset is NTAP and the holder was a declared foreign or temporary
    resident on the event date. Anything unknown stays assessable.
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


def disregard_attribution(account: 'Account | None', tap_status: str | None, event_date: date | None,
                          declared: residency.Periods | None = None,
                          mit_withheld: bool = False) -> tuple[bool, str | None]:
    """Return `(is_disregarded, reason)` for a trust-attributed gain (s855-40(2), s276-55).

    Disregarded if NTAP and the member was a foreign or temporary resident on the event date.
    A TAP gain is disregarded too when `mit_withheld` says the trust withheld MIT withholding
    tax on it (s840-815) and the member was a foreign resident, who pays no more on it.
    """
    if tap_status == TAP and mit_withheld:
        if declared is None:
            declared = residency.periods(account)
        if declared and residency.status_on(account, event_date, declared=declared) == residency.FOREIGN:
            return True, REASON_MIT_WITHHOLDING
        return False, None
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
