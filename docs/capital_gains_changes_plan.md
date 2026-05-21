# Plan: Implementing the 2027 Australian CGT Budget Changes in share-dinkum

> Background and a plain-English explanation of the proposed changes is in [`2027_capital_gains_tax_changes_are_really_complex.md`](./2027_capital_gains_tax_changes_are_really_complex.md). This document is the implementation plan.

---

## Context

[share-dinkum](https://github.com/pretoriusdre/share-dinkum) is a Django app for tracking share portfolios with a focus on Australian tax considerations (franking credits, AMIT cost base adjustments, CGT discount calculations).

The 2025/26 Federal Budget proposed three changes to the Australian CGT regime, effective **1 July 2027**:

1. **Abolish the 50% CGT discount** for gains accruing after 1 July 2027, replaced with **CPI cost-base indexation** (mirroring the pre-1999 regime).
2. **Minimum 30% effective tax rate** on real (indexed) capital gains accrued after 1 July 2027, with exemptions for income-support recipients.
3. **Transitional split** for assets bought before 1 July 2027 and sold after: 50% discount applies to the gain up to 1 July 2027 value; indexation applies to the gain from 1 July 2027 value to disposal.

https://budget.gov.au/content/factsheets/download/tax-explainers-negative-gearing-capital-gains-tax.docx

These changes are **not yet legislated**. If they become law, share-dinkum needs a major refactor.

### Why this needs to be rolled out carefully

The numbers share-dinkum produces feed directly into a user's tax return. Once a user has filed a return based on a figure the app showed them, that figure is "locked in" with the ATO. Two practical risks follow:

1. **Premature switching.** The reforms are *announced not legislated*. If we changed the default calculation now and the legislation is later amended or dropped, users who filed in the meantime would have used wrong numbers.
2. **Silent behaviour change.** Even after the legislation passes, a user who has seen one figure for years should not suddenly see a different one without understanding why. They need a side-by-side comparison and control over when to switch.

The plan therefore uses a **flag-gated, opt-in rollout** with **two gates doing different jobs**:

- **`sell.date` vs. `CGT_CUTOVER_DATE` (the *legal* gate).** The authoritative test of *which regime applies to a particular sale*. Once the law is in force, a sell before 1 July 2027 is always the old regime; on or after, always the new. Lives inside `cgt.compute_breakdown(sell_allocation)`. This is the only gate that should remain long-term.
- **`CGT_2027_REGIME_ENABLED` (the *rollout* gate).** A temporary global kill-switch deciding whether the new calculation path is *invoked at all*. While off, share-dinkum behaves exactly as today even for post-cutover sells, because the new code never runs. The risk it guards against: a user records a 2028-dated sell while the legislation is still in flight, files based on the number shown, and then has the calculation change before Royal Assent.

How the two gates compose:

- New data model and calculation code ship first with `CGT_2027_REGIME_ENABLED = False`, so they are dormant. This lets us build, test, and review without affecting any user's current output.
- Individual users can flip a per-`Account` "preview" flag to enable the new path for their account only, compare it to the current report, and validate it with their accountant before relying on it.
- Once the legislation is final, the global flag flips **ON** by default. The legal gate then does all the real work, and `CGT_2027_REGIME_ENABLED` can be removed in a later cleanup.

---

## Current State Summary (share-dinkum)

Currently, share-dinkum calculates gross capital gains, which can be combined with prior-year losses using basic ATO tools.

| Concern | Current implementation | File / Line |
|---|---|---|
| Cost base storage | Computed dynamically per parcel from `buy` + `cost_base_adjustment_allocation` | `models.py:925-937` |
| 50% discount | **Not stored, not reported**: only used transiently to *order* parcels under MIN_CGT strategy | `signals.py:88-95` |
| Capital loss tracking | **None**: `total_capital_gain` can be negative but is not aggregated or carried forward | `models.py:1073-1078` |
| Realised gain report | Per-allocation row, raw `capital_gain`, no taxable-gain column | `reports.py:1-43` |
| Parcel bifurcation | Eager: a sell splits the parcel into "sold" + "unsold" at sell time | `models.py:976-1027`, `signals.py:127-150` |
| AMIT cost base adjustments | Allocated weighted by `qty × days_held`, stored as `CostBaseAdjustmentAllocation` with an `activation_date` | `models.py:1171-1217`, `signals.py:162-217` |
| Price history | `InstrumentPriceHistory` (date, OHLC); already a usable source for 1-Jul-2027 market value | `models.py:697-718` |
| Account-level taxpayer config | Only `currency` and `fiscal_year_type`; **no `taxpayer_type`** | `models.py:141-150` |
| Constants | `CGT_DISCOUNT_RATE = 0.5`, `CGT_DISCOUNT_THRESHOLD_DAYS = 365` | `constants.py` |

The existing model relationships (`Buy → Parcel → SellAllocation`, with `CostBaseAdjustment`/`CostBaseAdjustmentAllocation` layering on top) are defined in `models.py` and unchanged by this plan.

**Key implication:**

Today, the 50% CGT discount is **not a field on any model** and **not a column in any report**. The realised-gain report shows the *raw* gain (`proceeds − cost_base`); if the parcel was held >365 days, the user (or their accountant) halves it themselves on the tax return. The discount is "implicit".

The 2027 regime makes that unworkable:

1. **The choice is no longer a simple yes/no.** Instead of "50% off if held >365 days", the rule branches on buy date and sell date relative to 1 July 2027 (Cases A / B / C). Case C splits a single sale into a pre-segment (discount eligible) and a post-segment (CPI-indexed, no discount) — not something a user can eyeball from a raw-gain figure.
2. **The numbers depend on data the app holds.** CPI factors, the market value at 1 July 2027, and the date of each cost-base adjustment all live in the database.

So the discount/indexation calculation has to move from "implicit, done by the user" to "explicit, done by the app", computed **per `SellAllocation`** and **per segment within an allocation**.

---

## Conceptual Model: Cutover at 1 July 2027

Let `D = date(2027, 7, 1)` (the cutover; lives in `constants.py` as `CGT_CUTOVER_DATE`). For each `SellAllocation` linking a `Parcel` (acquired at `buy.date`) to a `Sell` (at `sell.date`):

| Case | Buy date | Sell date | Treatment |
|---|---|---|---|
| **A**, Pre-cutover sell | any | < `D` | Unchanged. 50% discount if held >365 days (computed in report). |
| **B**, Pure post-cutover | ≥ `D` | ≥ `D` | New regime only. Cost base **CPI-indexed** from buy date to sell date. No discount. |
| **C**, Transitional (held across `D`) | < `D` | ≥ `D` | Two segments per sell allocation: <br/>• **Pre-segment gain** = `MV_D − cost_base` (50% discount eligible if held >365 days). <br/>• **Post-segment gain** = `proceeds − indexed(MV_D)` (no discount). |

See the forum doc's Case A/B/C diagrams for the visual walkthrough.

> **Scope note (minimum 30% tax rate).** The Budget's minimum-tax top-up is applied by the ATO against the taxpayer's overall income position. share-dinkum reports capital gains *separately* from regular income (the same way they're entered on the tax return), and any minimum-tax top-up is computed downstream by the ATO. **This plan does not implement the 30% minimum tax calculation**; the app's job is to output the correct taxable capital gain.

`MV_D` is the asset's market value at 1 July 2027: either (a) the close price on `D` from `InstrumentPriceHistory`, (b) a user-supplied valuation, or (c) the ATO's apportionment formula — straight-line growth between cost and proceeds: `cost_base + (proceeds − cost_base) × (D − buy.date) / (sell.date − buy.date)`.

---

## Design Changes

### 1. New constants (`constants.py`)
```python
from datetime import date
CGT_CUTOVER_DATE = date(2027, 7, 1)
# Flag for the regime; until legislated, default OFF
CGT_2027_REGIME_ENABLED = False
```
> Note: there is no `CGT_MINIMUM_TAX_RATE` constant. The 30% minimum tax rate is applied by the ATO at assessment time, not by this app.

### 2. Indexation factor source (`CPIIndex` table, or a flat rate)

The Budget paper says "Indexation will be calculated using CPI", but **every worked cameo assumes a flat 2.5% per annum**. It is not yet clear whether the final rule will be (a) actual published quarterly CPI, (b) a fixed statutory rate, or (c) capped CPI.

To keep the rest of the calculation independent of this choice, the indexation factor is exposed as a single function:

```python
# in cgt.py
def indexation_factor(from_date: date, to_date: date) -> Decimal: ...
```

Two pluggable backends, selected by a constant `CGT_INDEXATION_METHOD` in `constants.py` (`'CPI_TABLE'` or `'FLAT_RATE'`):

**Backend (a): `CPIIndex` table** (if actual CPI is used)
```python
class CPIIndex(models.Model):
    quarter_end_date = models.DateField(unique=True)  # e.g., 2027-06-30
    index_value = models.DecimalField(max_digits=10, decimal_places=4)
    source = models.CharField(max_length=64, default='ABS 6401.0 All Groups Australia')
```
- Loaded via the existing Excel `DataLoader` (add to `get_model_load_order()` in `loading.py`).
- `CPIIndex.get_factor(from_date, to_date)` returns `index(to) / index(from)`, using the *last published quarterly CPI on or before* each date (matching the ATO's pre-1999 methodology).

**Backend (b): flat rate** (if a statutory rate is used)
```python
# constants.py
CGT_FLAT_INDEXATION_RATE = Decimal('0.025')  # 2.5% p.a., the rate used in Budget cameos
```
- Factor = `(1 + rate) ** ((to_date − from_date).days / 365.25)`.

Until the legislation is final, **default to `'FLAT_RATE'` with 2.5%** because that is the rate the Budget paper actually used in its worked examples. The `CPIIndex` model still ships (the migration adds the table) so switching to backend (a) when legislation lands is a one-line constant change plus a data import. Backend (c) (capped CPI) is a thin layer on top of (a).

Both backends honour the "indexation never reduces cost base" rule by flooring the returned factor at `Decimal('1.0')`. Indexation also cannot create or increase a *loss*: if `proceeds < cost_base`, the loss is the *nominal* loss (`proceeds − cost_base`), not `proceeds − indexed_cost_base`.

### 3. New model: `MarketValueSnapshot`

Per-instrument valuation at the cutover (or any user-defined snapshot date). Lets users override the price-history-derived value with a specific ATO-acceptable valuation.
```python
class MarketValueSnapshot(BaseModel):
    instrument = FK(Instrument)
    snapshot_date = DateField()  # defaults to CGT_CUTOVER_DATE
    unit_value = MoneyField()
    source = CharField(...)  # 'price_history' | 'manual_valuation' | 'apportionment'
    class Meta:
        unique_together = [('account', 'instrument', 'snapshot_date')]
```
- Helper on `Parcel`: `market_value_at(d: date) -> Money` tries snapshot, then price history close, then apportionment formula.

> **Known limitation.** A snapshot is per `(instrument, date)`, so it forces *one* valuation method per instrument. The valuation-vs-apportionment choice is **not tax-neutral** — if an asset grew faster than the indexation rate before `D`, the valuation method yields more discount-eligible pre-segment; if slower, apportionment is better. If the legislation lets the taxpayer choose *per disposal*, this model can't express it without either two snapshots (valuation + apportionment) and a report-time selector, or moving the method choice down to the `SellAllocation` level. Deferred until the legislative wording is known; if the legislation mandates one method, the question goes away.

### 4. Extend `Account`
```python
TAXPAYER_TYPE_CHOICES = [
    ('INDIVIDUAL', 'Individual'),
    ('TRUST', 'Trust'),
    ('PARTNERSHIP', 'Partnership'),
    ('SMSF', 'Self-Managed Super Fund'),
    ('COMPANY', 'Company'),
    ('OTHER', 'Other'),
]
taxpayer_type = CharField(choices=..., default='INDIVIDUAL')
mv_default_method = CharField(  # 'PRICE_HISTORY' | 'APPORTIONMENT' | 'MANUAL_REQUIRED'
    choices=..., default='PRICE_HISTORY'
)
```
The discount only applies to INDIVIDUAL / TRUST / PARTNERSHIP. SMSFs already have a 1/3 discount (out of scope for v1; flag in code, leave existing 0.5 alone). No `receives_income_support` field is needed because the app does not compute the minimum-tax top-up.

### 5. Refactor `SellAllocation.total_capital_gain` into a computed object

The single scalar `total_capital_gain` is insufficient for the transitional case. Introduce a method returning a *structured result*:

```python
@safe_property
def cgt_breakdown(self) -> 'CGTBreakdown':
    """
    Returns a dataclass with:
      raw_gain, segment ('PRE'|'POST'|'TRANSITIONAL'),
      pre_segment_gain, post_segment_gain,
      discount_eligible_amount, indexed_amount,
      mv_at_cutover, indexed_cost_base,
      taxable_gain,
      reporting_buckets   # see below
    """
```
- Lives in a new `cgt.py` module (pure computation, no DB writes) — trivially testable against the Budget worked examples without standing up the Django ORM.
- `total_capital_gain` is retained as `cgt_breakdown.raw_gain` for backwards-compatibility with existing reports/tests.
- Gated by `CGT_2027_REGIME_ENABLED` and `sell.date >= CGT_CUTOVER_DATE`. While the flag is off, existing semantics are preserved.
- **The breakdown must tag each segment with its tax return reporting bucket** (see §6), not just its taxable amount, so an end-of-year report can aggregate many sales into the right rows. A Case C sale produces *two* tagged segments.

### 6. New report: `TaxableCapitalGainReport`

Extends (does not replace) `RealisedCapitalGainReport`. Columns:
`sell_date, instrument, qty, fiscal_year, segment, reporting_bucket, raw_gain, pre_segment_gain, post_segment_gain, mv_at_cutover, indexed_cost_base, discounted_gain, indexed_gain, capital_loss, fy_net_taxable_gain`.

`fy_net_taxable_gain` is the figure the taxpayer enters on their tax return under capital gains. The ATO then applies the 30% minimum rate test against that plus the taxpayer's other income — outside this app's scope.

**Reporting buckets.** Today every sale lands in exactly one of three method buckets (indexation / discount / other). After `D`, those buckets split along a pre-D / post-D axis, because the 30% minimum tax only bites on gains accruing on or after `D`:

| Reporting bucket | Source |
|---|---|
| Indexation method (pre-D) | Pre-1999 acquisitions sold < `D`, or the pre-segment of a Case C sale of a pre-1999 asset |
| Discount method (pre-D) | Case A held >12 months, and Case C pre-segments held >12 months |
| Other method (pre-D) | Case A held <12 months, and Case C pre-segments where total holding <12 months |
| Indexation method (post-D) | Case B held >12 months, and Case C post-segments |
| Other method (post-D) | Case B held <12 months |

The structurally new thing: **a single Case C sale produces entries in *two* buckets from one disposal** — a pre-D row and a post-D row. The report's aggregation must therefore roll up *segments*, not whole sales.

An FY-level aggregation layer (currently missing) is required for:
- Offsetting current-year capital losses against gains *before* applying discount/indexation (ATO ordering: losses first, then discount).
- Carrying forward unused losses.

### 7. Capital losses

Currently **un-modelled** and a precondition for correct indexation (you can't index a loss into a bigger loss). Add a new model:

```python
class CapitalLossCarryForward(BaseModel):
    fiscal_year = FK(FiscalYear)            # the FY in which the loss was incurred
    amount = MoneyField()                   # the loss available to use
    used_in_fiscal_year = FK(FiscalYear, null=True)  # FY where it was applied
    used_amount = MoneyField(default=0)
```

Populated by an FY-close action (`Account.close_fiscal_year(fy)`) that:
1. Sums all `SellAllocation.raw_gain` for the FY (treating negative as losses).
2. Offsets current-FY losses against current-FY gross gains.
3. Applies brought-forward prior losses next (against the *un-discounted, un-indexed* gain, per ATO order).
4. Then applies discount/indexation to the remaining gross gain per allocation/segment.
5. Records remaining losses as new `CapitalLossCarryForward` rows.

> **Loss-application optimisation (open design question).** ATO ordering fixes the *tiers* (current-year, then prior-year, then discount/indexation), but **within a tier the taxpayer chooses which gains each loss offsets**, and the choice changes the tax bill:
>
> | $1 of nominal loss applied against | Reduces taxable income by |
> |---|---|
> | Case A gain (50% discount-eligible) | $0.50 |
> | Case B indexed gain | $1.00 |
> | Case C pre-segment | $0.50 |
> | Case C post-segment | $1.00 |
>
> To minimise the legal tax bill, the FY-close routine should **sort gains by loss-application value within each ATO tier** and burn losses down the sorted list (burn against non-discount gains first). A naive "apply in document order" implementation reaches a legal-but-suboptimal answer. The optimisation itself is a simple greedy sort, but it is unconfirmed whether the taxpayer's free choice extends this far — **validate with an accountant before shipping anything that auto-picks an offset target.** This is the least certain piece of the plan.

### 8. Updated MIN_CGT parcel selection

`signals.py:88-95` must use the *new* taxable-gain formula when `sell.date >= CGT_CUTOVER_DATE`:
- Pure post-cutover: rank by `proceeds − indexed_cost_base` (no discount applied for sorting).
- Transitional: rank by `pre_segment_taxable + post_segment_taxable`.

Extract this into a function `cgt.estimate_taxable_gain_for_selection(parcel, sell)` so signals and reports share logic.

> **Performance note.** The new ranking is heavier than today's: every candidate parcel now needs MV_D and an indexed cost base computed *during* ranking, not just at report time. For a 120-parcel ETF holding, ranking on every sell could become noticeable. Cache `MV_D` per instrument and indexation factors per `(from_date, to_date)` pair across a single sell's ranking pass.

---

## Indexation of Cost Base Adjustments (AMIT and similar)

### Background

share-dinkum already supports mid-life cost base adjustments via `CostBaseAdjustment` and `CostBaseAdjustmentAllocation`. The dominant real-world case is **AMIT adjustments**, issued annually by Australian ETFs and managed funds:

- An **upward** adjustment occurs when the fund attributes taxable income exceeding the cash distribution. The retained amount becomes additional cost base.
- A **downward** adjustment is the reverse: the cash distribution exceeds attributed taxable income, reducing cost base (and if it would drive cost base negative, the excess becomes a current-year capital gain).

Each adjustment is date-stamped via `CostBaseAdjustmentAllocation.activation_date` (set to the start of the FY the adjustment relates to: `signals.py:178`).

The Budget paper does **not** mention AMIT or mid-life adjustments. This section reasons from precedent and the structure of the cost-base concept in ITAA 1997.

### Why "indexed from own date" is the most likely outcome

Under the pre-1999 indexation regime (which the 2027 reforms explicitly "mirror"), **each element of the cost base was indexed separately, from the date it was incurred.** ITAA 1997 s110-25 defines five cost-base elements, each with its own indexation start date. An AMIT cost base addition slots most naturally into element 4 ("capital expenditure to increase value") in spirit. Indexing a mid-life adjustment from the parcel's original buy date would *over-index* it (the money wasn't tied up in the asset for the full holding period), contradicting the Budget's stated rationale that indexation reflects real holding cost.

**Conclusion: each adjustment is indexed from its own `activation_date`, not from the parcel's buy date.** Because share-dinkum already date-stamps every `CostBaseAdjustmentAllocation`, **no data-model change is needed** — this is purely a calculation change. If the ATO instead decrees indexation from buy date, the fix is one line (pass `buy.date` instead of `activation_date`).

### Treatment per scenario

Let `D = 2027-07-01`. For each `CostBaseAdjustmentAllocation` linked to a parcel:

| Direction | `activation_date` | Parcel buy date | Sell date | Treatment |
|---|---|---|---|---|
| Upward | < `D` | any | < `D` | Old regime: included in `total_cost_base`, no indexation, 50% discount on gain. |
| Upward | < `D` | < `D` | ≥ `D` | **Transitional pre-segment**: included in `cost_base_at_D` used in `MV_D − cost_base_at_D`. No separate indexation (pre-segment uses the discount, not indexation). |
| Upward | ≥ `D` | < `D` | ≥ `D` | **Transitional post-segment only**: indexed from `activation_date` to sell date, added to indexed `MV_D`. No effect on the pre-segment. |
| Upward | ≥ `D` | ≥ `D` | ≥ `D` | **Pure post-cutover**: indexed from `activation_date` to sell date; summed with the original cost base indexed from `buy.date`. |
| Downward | any | any | any | Applied at face value, **no indexation** (consistent with pre-1999 treatment of negative components). |

For the "valuation" method of `MV_D`, pre-D adjustments are already reflected in the market price. For the "apportionment" method, the `cost` term is the cost base *as at D*, i.e. including all pre-D adjustments — which share-dinkum can compute exactly because every allocation's `activation_date` is recorded.

### Worked example

Parcel bought 1 Jul 2025 for $10,000. AMIT upward adjustment of $200 activated 1 Jul 2026, another $200 activated 1 Jul 2028; sold 1 Jul 2030 for $13,000. Flat 2.5% p.a. indexation, apportionment method for `MV_D`.

- **Cost base at D** = $10,000 + $200 (pre-D adjustment) = **$10,200**.
- **`MV_D` (apportionment)** = `cost + (proceeds − cost) × (D − buy)/(sell − buy)` = `$10,200 + ($13,000 − $10,200) × 2/5` = **$11,320**.
- **Pre-segment gain** = `MV_D − cost_base_at_D` = $11,320 − $10,200 = $1,120; held >365 days → taxable **$560**.
- **Post-segment indexed cost base** = indexed `MV_D` (`$11,320 × 1.025³ ≈ $12,191`) + indexed post-D adjustment (`$200 × 1.025² ≈ $210`) = **$12,401**.
- **Post-segment gain** = $13,000 − $12,401 = $599; no discount → taxable **$599**.
- **Total taxable = $560 + $599 = $1,159.**

Indexing everything from buy date instead of per-component would give a slightly higher tax. The difference is small for a single adjustment but becomes material for long-held ETFs with many AMIT adjustments.

### Implementation

`cgt.compute_indexed_cost_base(parcel, as_at_date, segment_start_date)` must:

1. Take the parcel's *original* cost base, index it from `buy.date` to `as_at_date` via `cgt.indexation_factor()`.
2. Iterate `parcel.cost_base_adjustment_allocation.filter(is_active=True, activation_date__gte=segment_start_date)`. For each: if upward, index from its `activation_date` to `as_at_date`; if downward, add at face value with no indexation.
3. Return the summed indexed cost base.

`segment_start_date` is `D` for the post-segment of a transitional case (pre-D adjustments are excluded — already in `cost_base_at_D`), and `parcel.buy.date` for the pure post-cutover case.

---

## Scenario Planning: Pre-Legislation

Because this is *announced not legislated*, the implementation ships as **two switchable layers**: the data model and plumbing now, the calculation gated off until the law is final. The scenarios that actually drive design choices:

| Scenario | Likelihood | Code response |
|---|---|---|
| Final indexation method is real CPI, not flat rate | Medium | Flip `CGT_INDEXATION_METHOD = 'CPI_TABLE'`, import the ABS CPI series. No code change elsewhere. |
| ATO mandates one MV method (valuation or apportionment) | Medium | Flip `Account.mv_default_method`; the per-allocation choice question (§3) disappears. |
| Postponed beyond 1 July 2027 | Medium | Change the single constant `CGT_CUTOVER_DATE`. |
| Repealed entirely | Low | `CGT_2027_REGIME_ENABLED = False` permanently; existing behaviour unchanged. |

**Principle:** ship the data model (CPI table, MV snapshots, taxpayer type, loss carry-forward) and the plumbing (`cgt_breakdown`) with `CGT_2027_REGIME_ENABLED = False` so reports behave exactly as today. Add a per-`Account` "preview mode" toggle for users who want to model the impact ahead of time.

---

## Unknowns / Risks

1. **Indexation source: real CPI vs. flat rate.** Budget policy text says "CPI", but every cameo uses flat 2.5% p.a. The plan ships both backends behind `cgt.indexation_factor()`; v1 defaults to flat 2.5%. **Verify when ATO publishes guidance.**
2. **MV valuation method.** Budget paper lets the taxpayer choose valuation or apportionment; the apportionment formula's exact form is not published in detail. The plan assumes straight-line growth `cost + (proceeds − cost) × (D − buy)/(sell − buy)`. **Verify when ATO publishes guidance.** Whether the choice can be made per disposal is also open (see §3).
3. **Indexation of cost-base adjustments.** No Budget guidance on AMIT. The "per-component, from own date" treatment is plausible but speculative — **flag clearly in user-facing report notes.**
4. **Transitional-asset loss interactions.** Can a loss in the post-segment offset a gain in the pre-segment of the *same* Case C sale? Assume yes (economically one disposal), but the ATO may rule otherwise. The loss-application *optimisation* within a tier (§7) is also unconfirmed — validate with an accountant.
5. **SMSF 1/3 discount.** Not modelled at all today (default 0.5 is wrong for SMSFs even now). Out of scope for v1; `taxpayer_type` is added but the SMSF rate is left alone. Flag in code comments.
6. **Foreign-currency parcels.** Cost base is held in account currency after conversion; CPI applied is Australian CPI regardless of instrument currency. Should "just work" but warrants a test.
7. **Pre-1985 assets.** Budget paper says pre-1985 gains *before* `D` remain exempt; gains *after* `D` are not. Currently impossible to represent cleanly. Out of scope; add a note.
8. **Share splits crossing the cutover.** `Parcel.cumulative_split_multiplier` already handles quantity scaling. Verify the `MV_D` lookup uses the *split-adjusted* historical close.

---

## Critical Files to Modify

| File | Change |
|---|---|
| `share_dinkum_app/constants.py` | Add `CGT_CUTOVER_DATE`, `CGT_2027_REGIME_ENABLED`, `CGT_INDEXATION_METHOD`, `CGT_FLAT_INDEXATION_RATE` |
| `share_dinkum_app/models.py` | Add `CPIIndex`, `MarketValueSnapshot`, `CapitalLossCarryForward`; extend `Account` with `taxpayer_type`, `mv_default_method`; add `Parcel.market_value_at()` |
| `share_dinkum_app/cgt.py` *(new)* | Pure-function module: `CGTBreakdown` dataclass, `compute_breakdown(sell_allocation)`, `compute_indexed_cost_base(parcel, as_at)`, `estimate_taxable_gain_for_selection(parcel, sell)`, `indexation_factor(from_date, to_date)` with pluggable backend |
| `share_dinkum_app/signals.py` | Update MIN_CGT to call `cgt.estimate_taxable_gain_for_selection` when `sell.date >= CGT_CUTOVER_DATE` and flag enabled |
| `share_dinkum_app/reports.py` | Add `TaxableCapitalGainReport`; keep existing `RealisedCapitalGainReport` unchanged |
| `share_dinkum_app/loading.py` | Add `CPIIndex` and `MarketValueSnapshot` to `DataLoader.get_model_load_order()` |
| `share_dinkum_app/migrations/0014_*.py` | New migration for the above |
| `share_dinkum_app/tests.py` | New test class `Test2027CGTRegime` (see verification) |

Reused existing code:
- `Parcel.total_cost_base` (`models.py:925`): feeds `compute_breakdown`.
- `InstrumentPriceHistory` (`models.py:697`): default source for `MarketValueSnapshot`.
- `ExchangeRate.apply` (`models.py:291`): pattern for `CPIIndex.apply(Money) -> Money`.
- `DataLoader.load_table_to_model` (`loading.py:142+`): Excel import for CPI data.
- `FiscalYearType.classify_date` (`models.py:64`): for FY-close routine.
- `safe_property` decorator (used throughout `models.py`): for the new `cgt_breakdown` property.

---

## Verification

End-to-end tests using the existing `TransactionTestCase` + factory pattern in `tests.py`:

1. **Backward compatibility (flag OFF)**: All existing tests pass unchanged. `RealisedCapitalGainReport` output is bit-identical for all sells, including those dated after 2027-07-01.
2. **Pre-cutover only (Case A)**: Buy 2024-01-01, sell 2025-03-15. With flag ON, `cgt_breakdown.segment == 'PRE'` and result matches the existing report.
3. **Pure post-cutover (Case B)**: Buy 2028-01-01 @ $100, sell 2030-01-01 @ $120, CPI 100→105. `indexed_cost_base == $105`, `real_gain == $15`. No discount applied.
4. **Transitional (Case C), Budget paper "Jane" example**: Buy 2022-07-01 @ $800,000, sell 2032-07-01 @ $1,600,000. `MV_D` via apportionment = $1,131,371. Pre-segment gain = $331,371; 50%-discounted = $165,685. Post-segment taxable = $319,958. Total taxable = $485,643 ± rounding. **Reproduce this exact figure** as the canonical regression test.
5. **Budget paper "Zoe" example**: Buy 2027-07-01 @ $100, sell 2032-07-01 @ $125, 2.5% inflation. Indexed cost base ≈ $113, taxable gain ≈ $12.
6. **Capital loss interaction**: Two sells in FY2028: gain $10,000 (indexed) + loss $4,000 (nominal). FY-close produces net taxable $6,000 (loss applies before indexation/discount).
7. **Carried-forward loss**: Loss $5,000 in FY2028, gain $15,000 (indexed) in FY2029. FY2029 net taxable = $10,000; `CapitalLossCarryForward.used_amount == $5,000`.
8. **AMIT adjustment crossing cutover**: Parcel bought 2025, AMIT adjustment activated 2028 ($50), sold 2030. Pre-segment uses cost base *excluding* the post-D adjustment; post-segment indexes the $50 from 2028→2030.
9. **MIN_CGT parcel selection post-cutover**: Two parcels (one bought 2026, one bought 2028); sell in 2029. Selection ranks by new-regime taxable gain, not the old 50%-discounted gain.

> The Budget paper's "Jack" worked example (minimum 30% top-up) is *not* a test case for share-dinkum: that calculation belongs to the ATO assessment system, not the portfolio tracker.

Manual / UI verification (after tests pass):
- Run `python manage.py runserver`; create an Account; import CPI data via Excel; verify a transitional sell renders the new breakdown columns.
- Export the report; re-import it; verify round-trip integrity (per existing `DataExport`/`DataImport` pattern).
- Toggle `CGT_2027_REGIME_ENABLED = False`, refresh the report; confirm columns and totals revert.
