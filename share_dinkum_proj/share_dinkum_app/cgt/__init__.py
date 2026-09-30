"""Capital gains tax characterisation. Reads the database, never writes to it.

Models store the mechanical figures (proceeds, cost base); this package works out how they
are taxed, so a correction changes reports but not stored data.

* Cost base: purchase, brokerage and cost base adjustments.
* Division 115 discount by taxpayer type, apportioned for days abroad (s115-105).
* Gains disregarded for foreign or temporary residents (s855-10, s768-915, s855-40(2)).
* From 1 July 2027: CPI indexation (Division 114), the s112-155 deemed sale, and the
  s102-5 statutory loss order.
* The s102-5 method statement.

With no residency declared, the flat discount applies and reports say so.

Not implemented (flagged or raised, not guessed): the s112-185 apportioning method (a
straddling disposal needs a market valuation), Subdivision 26-155 quarantining, the
s115-115(4) election, and the s119-10(2) minimum tax gap amount.

Not implemented and not flagged: CGT event I1 on departure without a s104-165(2) election,
the s855-45 market value cost base on becoming a resident, and the pre-CGT exemption for
assets acquired before 20 September 1985. Valuations for the first two can be recorded, but
nothing reads them yet.
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
