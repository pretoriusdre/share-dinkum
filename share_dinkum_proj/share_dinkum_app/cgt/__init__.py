"""Capital gains tax calculation.

Everything that characterises a capital gain for tax purposes lives here, and nothing here
writes to the database. Models store gross, mechanical figures -- proceeds, cost base, the
difference between them. How that difference is *taxed* depends on residency, on the asset,
and on when the event happened, all of which change over time and none of which should be
baked into a stored value.

Keeping the characterisation separate is what makes a correction safe: improving a
calculation changes what a report says, but nothing already recorded. Where that shifts a
figure for a year the user may already have lodged, CGTReturnSnapshot and
CGTBasisChangeReport exist to make the movement visible rather than silent.

Current scope
-------------
* The cost base is the parcel's own build-up: purchase, brokerage, cost base adjustments.
* The discount follows Division 115, including the s115-105 apportionment for days spent
  abroad and the rate differences between taxpayer types.
* Gains a foreign or temporary resident can disregard under s855-10, s768-915 and
  s855-40(2) are identified as such.

All of that depends on the account declaring who and where the holder is. An account that
has declared nothing keeps the flat 50% the application has always applied, and every
report built on it says so. Declaring an unbroken period of Australian residency reproduces
the same figures exactly, so the declaration is free for the users it does not affect.

* From 1 July 2027 the discount gives way to CPI indexation of the cost base (Division 114),
  a holding that straddles the cutover is split in two by the deemed sale of s112-155, and
  the s102-5 method statement absorbs losses in the statutory order.

Not yet implemented, and each raises or flags rather than guessing: the s112-185 apportioning
method, which has not been made, so a straddling disposal needs a market valuation; the
residential categories of s102-6 and the Subdivision 26-155 quarantining that goes with them;
the s115-115(4) market value election; and the `minimum tax gap amount` of s119-10(2), which
needs income this application does not hold.
"""

from share_dinkum_app.cgt.discount import (
    apply_discount,
    apportionment_fraction,
    base_rate,
    discount_percentage,
    is_discount_eligible,
)
from share_dinkum_app.cgt.residency import (
    BASIS_DIVISION_115,
    BASIS_LEGACY,
    basis as residency_basis,
    resident_days,
    status_on as residency_status_on,
)
from share_dinkum_app.cgt.cutover import (
    deemed_reset_dates,
    deemed_sale_applies,
)
from share_dinkum_app.cgt.indexation import (
    IndexationDataUnavailable,
    indexation_factor,
    is_indexation_eligible,
)
from share_dinkum_app.cgt.schedule import build as build_schedule
from share_dinkum_app.cgt.tap import (
    disregard,
    disregard_attribution,
    parcel_tap_status,
)
from share_dinkum_app.cgt.classification import (
    asset_category,
    is_real_property,
    suggest_legal_form,
    suggest_legal_form_from_activity,
    suggested_country,
)
from share_dinkum_app.cgt.events import (
    CGTEvent,
    all_events,
    attribution_events,
    disposal_events,
    event_fields,
)

__all__ = [
    'CGTEvent',
    'all_events',
    'attribution_events',
    'asset_category',
    'is_real_property',
    'suggest_legal_form',
    'suggest_legal_form_from_activity',
    'suggested_country',
    'disposal_events',
    'event_fields',
    'apply_discount',
    'apportionment_fraction',
    'base_rate',
    'discount_percentage',
    'is_discount_eligible',
    'BASIS_DIVISION_115',
    'BASIS_LEGACY',
    'residency_basis',
    'residency_status_on',
    'resident_days',
    'build_schedule',
    'deemed_reset_dates',
    'deemed_sale_applies',
    'IndexationDataUnavailable',
    'indexation_factor',
    'is_indexation_eligible',
    'disregard',
    'disregard_attribution',
    'parcel_tap_status',
]
