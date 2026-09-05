# Plan: Implementing the 2027 Australian CGT changes in share-dinkum

> Revised against the enacted law. Earlier drafts of this plan were written when the
> measure was an unlegislated Budget announcement; several design choices were hedges
> against uncertainty that no longer exists, and several conditions in the final Act were
> not anticipated at all. Background reading is in
> [`2027_capital_gains_tax_changes_are_really_complex.md`](./2027_capital_gains_tax_changes_are_really_complex.md)
> and [`capital_gains_changes.md`](./capital_gains_changes.md), both of which predate the
> legislation and should be read as history rather than specification.

---

## Status: legislated

**Treasury Laws Amendment (Tax Reform No. 1) Act 2026** — Act No. 49 of 2026, Royal Assent
**26 June 2026**. Full text: [legislation.gov.au/C2026A00049](https://www.legislation.gov.au/C2026A00049/latest/text).
Explanatory Memorandum and Bills Digest via the
[Bill homepage](https://www.aph.gov.au/Parliamentary_Business/Bills_Legislation/Bills_Search_Results/Result?bId=r7493).

For CGT events on or after **1 July 2027**, the 50% CGT discount is replaced for
individuals, trusts and partnerships by **CPI cost base indexation** plus a **30% minimum
tax** on capital gains.

| Provision | Effect |
|---|---|
| **s110-36(1A)** | Indexation for Australian resident individuals and trusts, CGT events on/after 1 July 2027 |
| **Division 114** | Indexation machinery. **s114-25** residency test, **s114-30** asset test |
| **Subdivision 112-E** (ss112-155, 112-165, 112-175) | Deemed sale just before, and reacquisition on, 1 July 2027 |
| **s112-160** | Defers the gain/loss from the deemed sale until the later realisation event |
| **s112-185** | Minister may determine an apportioning method by legislative instrument |
| **s102-6** | Defines the four new capital gain categories |
| **s102-5** | Net capital gain method statement, now **7 steps** |
| **s115-100** amended | Discount percentage. New paragraph **(f): 0% if no other paragraph applies** |
| **s115-102 / s115-125** | 50% retained for new residential dwellings; up to 60% for affordable housing |
| **Division 119** | 30% minimum tax (ss119-5, 119-10, 119-15) |
| **s960-275(1B)** | Indexation factor runs only from the quarter starting 1 July 2027 |

---

## What changed from the previous plan

Seven substantive corrections. Items 1–3 are new requirements the plan did not model at
all; items 4–5 remove work the plan proposed; items 6–7 resolve open questions.

### 1. Residency conditions — entirely absent from the previous plan, now mandatory

The Act gates both indexation and the transitional reset on residency. Neither condition
was anticipated.

**s114-25(2)** — indexation eligibility:

> You must be neither a foreign resident nor a \*temporary resident at any time during the
> period (the **testing period**): (a) starting on the later of **1 July 2027** and the day
> of \*acquiring the \*CGT asset; and (b) ending on the day the \*CGT event happens.

Note the testing period **starts at 1 July 2027 at the earliest** — it does not look back
over the whole ownership period. Residency before the cutover is irrelevant to indexation.

**s112-155(1)(d)** — the deemed sale and reacquisition is denied where s115-105
(foreign/temporary residents) would apply to the gain. The section is headed "Australian
resident individuals". The EM: it "cannot be used to restore or increase a foreign or
temporary resident's entitlement to the CGT discount."

**Implication for the data model.** `Account` needs residency periods, not a single flag —
the tests are about status *over a period*, and an account holder's status can change. This
is a new model, not a field:

```python
class ResidencyPeriod(BaseModel):
    account = FK(Account)
    start_date = DateField()
    end_date = DateField(null=True, blank=True)   # null = ongoing
    status = CharField(choices=[
        ('RESIDENT', 'Australian resident'),
        ('FOREIGN', 'Foreign resident'),
        ('TEMPORARY', 'Temporary resident'),
    ])
```

with helpers `Account.was_foreign_or_temporary_between(start, end) -> bool` and
`Account.is_indexation_eligible(acquisition_date, cgt_event_date) -> bool` implementing
the s114-25 testing period directly.

~~Default for existing accounts: a single `RESIDENT` period with `start_date` = account
creation and no end date, preserving today's behaviour for the overwhelming majority.~~

**Superseded — this default was not implemented, and should not be.** `created_at` is a
software timestamp later than most users' earliest `Buy`, so it leaves every earlier parcel
in a fabricated gap; it writes a legal assertion the user never made, which then flows into
the Excel export and back in with the appearance of provenance; it changes historical figures
for precisely the people it is wrong about; and it cannot represent the s115-105(2)(e)
reach-back to 8 May 2012 at all.

**The migration writes nothing.** Absence of data means *undeclared*, not *resident*: an
account with no `ResidencyPeriod` rows keeps the flat 50% it has always had, and every report
built on it says so. Declaring one unbroken period of Australian residency reproduces every
existing figure exactly, because resident days equal total days. That is asserted by
`ResidencyInertForResidentsTests`, which is the load-bearing test for the whole feature.

> **Note on s115-105.** share-dinkum does not currently implement the existing
> foreign-resident discount apportionment (s115-105/115-115) at all — the app applies a
> flat 50% via `CGT_DISCOUNT_RATE`. That is already incorrect for any account holder who
> has spent time overseas since 8 May 2012, independently of the 2027 changes. Because
> s112-155(1)(d) now keys off whether s115-105 applies, this gap has to be closed as part
> of this work rather than deferred.

### 2. The transitional case is a statutory deemed disposal, not a conceptual split

The previous plan modelled Case C as an arithmetic split computed at report time. The Act
does something legally different: a **deemed sale on 30 June 2027 and reacquisition on
1 July 2027** (s112-155(2)), with the resulting gain or loss **deferred** until the actual
realisation event (s112-160).

Practical consequences the arithmetic-split model does not capture:

- The deferred gain is a **distinct gain with its own category** (deferred residential /
  deferred non-residential), not a component of the later gain. It is reported separately
  and absorbs losses in a different order (see item 4).
- The reacquisition on 1 July 2027 resets the acquisition date for **indexation** purposes
  (s960-275(1B)) but is **disregarded for the 12-month rule** (s114-10(2) and (9)), so a
  parcel bought in 2020 and sold in 2028 is still "held >12 months" for both slices.
- The choice of valuation vs apportionment is made **when lodging the return for the year
  of the realisation event** (s103-25, and ss112-155(4)) — *not* in 2027. Users do not have
  to decide anything at the cutover.

That last point materially simplifies the UX: the app does not need to force a decision in
June 2027, and can offer the comparison at sale time.

### 3. Four gain categories now have to be tracked

**s102-6** defines: `residential capital gain`, `non-residential capital gain`,
`deferred residential capital gain`, `deferred non-residential capital gain`. Every gain
the app produces must be tagged with one.

**For a share portfolio tracker this is simpler than it looks**: shares and ETFs are never
residential dwellings, so share-dinkum only ever produces **non-residential** and
**deferred non-residential** gains. The residential categories and the `26-155` quarantined
amounts (steps 3 and 4 of the method statement) can be implemented as always-zero and
documented as out of scope, with an explicit guard that raises if a residential asset is
ever introduced.

This replaces the previous plan's bespoke "reporting buckets" table (§6), which was an
invented taxonomy. Use the statutory categories instead — they are what the tax return now
asks for.

### 4. Loss-offset ordering is now mandated — remove the optimiser

The previous plan's §7 proposed a greedy optimiser that sorted gains by "loss-application
value" and burned losses against non-discounted gains first, flagged as "the least certain
piece of the plan". **The Act settles it, and not in that direction.** s102-5 Step 1:

> (a) first, reduce any \*deferred non-residential capital gains;
> (b) then, reduce any \*deferred residential capital gains;
> (c) then, reduce any \*non-residential capital gains;
> (d) then, reduce any \*residential capital gains.

Step 2 applies prior-year losses "in the same order". The order is statutory and runs
*against* the taxpayer — deferred gains (which carry the 50% discount) absorb losses first,
which is the least valuable use of a loss.

Discretion survives only **within** a category (Step 1, Note 3: "If you have more than one
capital gain within a category … you can choose the order in which you reduce them"). For a
share portfolio where everything is non-residential, that discretion is nearly worthless,
because gains within a category are taxed alike.

**Action: delete the optimiser from the plan.** Implement the statutory order literally.
This removes the plan's single biggest correctness risk and its "validate with an
accountant before shipping" caveat.

### 4a. Indexation and the discount are mutually exclusive, with no election to model

**s110-36(1A)** is mandatory — "the cost base *also includes* indexation … if" the Division
114 conditions are satisfied. **s115-20(1)(a)** then denies discount treatment to any gain
"worked out using a cost base that has been calculated with reference to indexation". The
two are separated by their own conditions and there is no user choice between them. Any UI
offering one is modelling something the Act does not contain.

### 4b. s112-155(1)(d) is far broader than "foreign residents"

It denies the 2027 deemed sale wherever s115-105 *would* apply, and **s115-105(2)(e)**
catches anyone who was a foreign **or temporary** resident during any part of the ownership
period after 8 May 2012. Returned expatriates and former 482 visa holders are caught
permanently, on assets they held at the time — including people who are Australian residents
today and have been for years.

### 4c. The returned-expat trap — a harsh edge the plan does not identify

Resident before 1 July 2027, foreign or temporary at some point after 8 May 2012:

* no deemed sale (s112-155(1)(d)), so **no 50% is banked** on pre-2027 growth;
* resident from the cutover, so s114-25 is satisfied and indexation applies from 1 July 2027;
* and therefore **no discount at all**, because s115-20 denies it on an indexed cost base.

They lose the discount with nothing replacing it but relief running from 2027 on growth that
mostly happened before then. Implemented, and explained on the row: `ReturnedExpatIndexationTests`
asserts it, including a comparison against the same portfolio held by someone who never left,
because the figure alone does not show what it cost.

### 4d. A capital loss is never indexed — found in implementation, in neither plan

**s110-55** excludes indexation from the reduced cost base, and **s100-45** and **s104-10(4)**
work a capital loss out against that. Indexing the cost base on the loss side manufactures a
deductible loss out of an asset that merely failed to keep pace with inflation.

This also produces a **third outcome** that a gain-or-loss model has nowhere to put: where
the proceeds fall between the plain and the indexed cost base there is no gain *and* no loss.
`cgt/events.py: _outcome()` returns all three. It was found by an unrelated test reporting a
loss of $2,450 on a holding that fell $2,000 — which is the shape this class of bug takes: not
an obviously wrong number, just a slightly larger one, in the taxpayer's favour.

### 5. Indexation source is settled — CPI, not a flat rate

The previous plan shipped two backends and defaulted to a flat 2.5% because that was the
rate in the Budget cameos. The Act uses actual CPI through Subdivision 960-M, with
**s960-275(1B)** fixing the earliest quarter as the one starting 1 July 2027.

**Action:** default `CGT_INDEXATION_METHOD = 'CPI_TABLE'`. Keep the flat-rate backend, but
demote it to a testing and projection aid rather than the production default. The `CPIIndex`
model as previously specified is still correct.

### 5a. `CGT_DISCOUNT_THRESHOLD_DAYS = 365` is wrong, independently of the reform

**s115-25** requires the asset to have been acquired "at least 12 months before" the CGT
event. That is a calendar comparison, not a day count, and counting 365 days made the answer
depend on whether a leap day happened to fall inside the holding period: an asset bought on
1 March and sold the following 1 March qualified in one year and not in another. It was also
compared with `>` at `signals.py:93`. Replaced by a calendar test in `cgt/discount.py`, which
changes which parcels the MIN_CGT strategy selects.

### 6. Pre-CGT assets are now representable

The previous plan listed pre-1985 assets as "currently impossible to represent cleanly;
out of scope". **s112-175** now gives them the same deemed sale and reacquisition, with
the pre-1 July 2027 gain disregarded (s112-175(2)). That is straightforward to implement
using the same machinery as s112-155 — the only difference is that the deferred gain is
disregarded rather than deferred. Worth bringing into scope.

### 7. The 30% minimum tax — partially in scope after all

The previous plan excluded Division 119 entirely on the basis that the ATO applies it at
assessment. That is half right. **s119-5** defines your `minimum tax capital gain` as the
gains remaining after step 6 of the s102-5 method statement, reduced by Division 30/31
deductions — **that base is computable from portfolio data and the app should output it.**

What remains outside scope is the `minimum tax gap amount` (s119-10(2)), which depends on
the taxpayer's total taxable income and marginal rates.

Also note **s119-10(1)(b)**: the extra tax applies if "you are an Australian resident **at
any time during the income year**". A part-year resident is caught. This interacts with the
`ResidencyPeriod` model from item 1.

---

## Rollout gating — revised rationale

The previous plan's two gates were justified by the risk that "the reforms are announced
not legislated" and might be amended or dropped. That risk is gone. Both gates are still
worth keeping, for different reasons:

- **`sell.date` vs `CGT_CUTOVER_DATE` (the legal gate).** Unchanged and now definitive.
  A CGT event before 1 July 2027 is the old regime; on or after, the new one. Lives in
  `cgt.compute_breakdown(sell_allocation)`. Permanent.
- **`CGT_2027_REGIME_ENABLED` (the rollout gate).** Retained, but its job is now staged
  verification rather than legal hedging. The genuine remaining uncertainty is the
  **s112-185 legislative instrument**, which has not been made — the apportioning method is
  delegated to the Minister and its exact form is unknown. Until it exists, any
  apportionment output is a projection.

Keeping a kill-switch is still justified because the numbers feed tax returns, and because
a user who has seen one figure for years should not see it change silently. But the plan
should no longer describe the reform itself as conditional.

**Suggested sequencing:**

1. Ship `ResidencyPeriod` and s115-105/115-115 apportionment first, gated off. This fixes an
   existing correctness bug and is a prerequisite for the 2027 conditions.
2. Ship the data model and `cgt.py` with `CGT_2027_REGIME_ENABLED = False`.
3. Per-`Account` preview flag for users who want to model ahead.
4. Flip the global default ON once the s112-185 instrument is registered and the app's
   apportionment matches it.

---

## Conceptual model, restated against the Act

Let `D = date(2027, 7, 1)` (`CGT_CUTOVER_DATE` in `constants.py`).

| Case | Buy | Sell | Treatment |
|---|---|---|---|
| **A** | any | `< D` | Unchanged. Discount per Division 115 — **including s115-105 apportionment where the holder was foreign/temporary resident**, which the app does not currently implement |
| **B** | `≥ D` | `≥ D` | Indexation from buy date, if s114-25 and s114-30 are satisfied. No discount. `non-residential capital gain` |
| **C** | `< D` | `≥ D` | Deemed sale at `D` (s112-155) **if the holder is not caught by s115-105**. Produces a `deferred non-residential capital gain` (discount-eligible) plus a `non-residential capital gain` on post-`D` growth (indexation-eligible if s114-25 met) |
| **C′** | `< D` | `≥ D` | Holder **is** caught by s115-105: **no deemed sale**. Single gain under whatever Division 115 gives them, no indexation. See open question below |

`MV_D` sources are unchanged from the previous plan — snapshot, then price history close,
then the s112-185 apportioning method once it exists.

> **Resolved — was an open question, and is not one.** For Case C′ the Act was read as
> silent on what discount percentage applies to a post-`D` disposal, and the plan proposed
> implementing both readings behind `CGT_FOREIGN_RESIDENT_DISCOUNT_SURVIVES_CUTOVER`.
>
> That constant is not needed and was never written. **s115-100(c)** — unamended, and
> therefore invisible in the amending Act, which is why it was missed — reads:
>
> > (c) the percentage resulting from section 115-115 if section 115-105 or 115-110 applies to the gain; or
>
> Paragraph (f)'s 0% applies only "if none of the above paragraphs applies". Where s115-105
> applies, paragraph (c) applies, so (f) is never reached. **The apportioned discount
> survives 1 July 2027.** One behaviour, implemented in `cgt/discount.py`.
>
> The general lesson, which cost this plan four of its errors: an amending Act shows only
> what it changes. Anything load-bearing has to be read from the consolidated compilation.

---

## Design changes

Items unchanged from the previous plan are listed for completeness without restating
detail; see git history for the superseded text.

| # | Component | Status |
|---|---|---|
| 1 | `CGT_CUTOVER_DATE`, `CGT_2027_REGIME_ENABLED`, `CGT_INDEXATION_METHOD` | **Changed** — default `CGT_INDEXATION_METHOD` to `'CPI_TABLE'` |
| 2 | `CPIIndex` model + `cgt.indexation_factor()` | Unchanged. Honour s960-275(1B): earliest quarter is that starting 1 July 2027 |
| 3 | `MarketValueSnapshot` | Unchanged. The per-disposal choice concern is resolved — s103-25 makes the choice at lodgment for the realisation year, so store both and select at report time |
| 4 | `Account.taxpayer_type`, `mv_default_method` | Unchanged |
| 4a | **`ResidencyPeriod` model** | **New** — required by s114-25 and s112-155(1)(d) |
| 5 | `SellAllocation.cgt_breakdown` → `CGTBreakdown` dataclass | **Changed** — tag with statutory s102-6 categories, not bespoke buckets; add `deferred_gain` as a first-class field |
| 6 | `TaxableCapitalGainReport` | **Changed** — columns keyed to the four s102-6 categories and the 7-step s102-5 statement; add `minimum_tax_capital_gain` |
| 7 | `CapitalLossCarryForward` + FY-close | **Changed** — implement the statutory Step 1 (a)–(d) ordering; **delete the greedy optimiser** |
| 8 | MIN_CGT parcel selection | Unchanged in intent. Note `signals.py:93` currently hardcodes a flat 50% and is already wrong for foreign residents |

### Cost base adjustments (AMIT)

The previous plan's reasoning — that each adjustment is indexed from its own
`activation_date` rather than the parcel's buy date, by analogy with the per-element
indexation of s110-25 — **survives the legislation and is now better supported**.
s112-185(2)(b) requires the apportioning method to take into account "any expenditure
(including indexation) in an element of the cost base … on or after 1 July 2027", which
is per-element, date-aware language.

The treatment table and worked example in the previous version remain valid. No data model
change is needed, because `CostBaseAdjustmentAllocation.activation_date` is already
recorded.

---

## Critical files to modify

| File | Change |
|---|---|
| `share_dinkum_app/constants.py` | `CGT_CUTOVER_DATE`, `CGT_2027_REGIME_ENABLED`, `CGT_INDEXATION_METHOD='CPI_TABLE'`, `CGT_FLAT_INDEXATION_RATE`, `CGT_FOREIGN_RESIDENT_DISCOUNT_SURVIVES_CUTOVER` |
| `share_dinkum_app/models.py` | Add `CPIIndex`, `MarketValueSnapshot`, `CapitalLossCarryForward`, **`ResidencyPeriod`**; extend `Account`; add `Parcel.market_value_at()` |
| `share_dinkum_app/cgt.py` *(new)* | `CGTBreakdown`, `compute_breakdown()`, `compute_indexed_cost_base()`, `indexation_factor()`, `estimate_taxable_gain_for_selection()`, **`discount_percentage(parcel, sell, account)` implementing Division 115 including s115-105/115-115** |
| `share_dinkum_app/signals.py` | MIN_CGT via `cgt.estimate_taxable_gain_for_selection`; replace the hardcoded 50% at `signals.py:93` |
| `share_dinkum_app/reports.py` | Add `TaxableCapitalGainReport` keyed to s102-6 categories; keep `RealisedCapitalGainReport` and `OpenParcelReport` unchanged |
| `share_dinkum_app/loading.py` | Add `CPIIndex`, `MarketValueSnapshot`, `ResidencyPeriod` to `get_model_load_order()` |
| `share_dinkum_app/tests.py` | `Test2027CGTRegime`, `TestForeignResidentDiscount` |

---

## Verification

Retain tests 1–9 from the previous plan, with these changes and additions:

- **Test 4 and 5 (Budget "Jane" and "Zoe" cameos)** — these came from the Budget factsheet,
  not the Act. Re-derive expected values from the EM's worked examples instead, and treat
  any apportionment figure as provisional until the s112-185 instrument is made.
- **New: s114-25 testing period.** Account foreign-resident 2021–2026, resident from 2026.
  Asset bought 2020, sold 2030. Indexation **is** available: the testing period starts
  1 July 2027 and the holder is resident throughout it. Guards against the intuitive-but-wrong
  reading that past non-residency disqualifies.
- **New: s114-25 failure.** Same account, but foreign-resident for one week in 2028.
  Indexation denied entirely — no apportionment, no partial credit.
- **New: s112-155(1)(d).** Account caught by s115-105 → no deemed sale, no `deferred_gain`
  produced, single gain in the `non-residential` category.
- **New: statutory loss ordering.** FY with a deferred non-residential gain, a
  non-residential gain and a loss. Assert the loss hits the **deferred** gain first, per
  s102-5 Step 1(a) — i.e. assert the taxpayer-unfavourable statutory order, not the
  optimised one.
- **New: 12-month rule across the deemed sale.** Bought 2020, sold Aug 2027. Assert both
  slices are treated as held >12 months (s114-10(2), (9)) despite the 1 July 2027
  reacquisition.
- **New: s115-105 apportionment.** Independent of the 2027 changes — assert the discount
  percentage for a part-period foreign resident is `50% × resident days ÷ total days`, not
  a flat 50%.

---

## Open items

1. **s112-185 legislative instrument not yet made.** The apportioning method is delegated
   to the Minister. Until registered, the straight-line formula is a placeholder. This is
   now the single largest unknown and the gate on flipping the default ON.
2. ~~**Case C′ discount percentage**~~ — **closed.** s115-100(c) resolves it from the text;
   see the box above. The apportioned discount survives the cutover.
3. **Treasury has signalled further amendments.** EM (s114-25 discussion): "there is an
   intention to further consider how these amendments apply to entities that are Australian
   residents for only part of the period in which they hold a CGT asset." Expect the
   part-year residency rules to move.
4. ~~**SMSF 1/3 discount**~~ — **closed.** `Account.taxpayer_type` now carries it, along with
   nil for a company. The default is `UNDECLARED`, which keeps the flat 50% and says so,
   because a wrong guess here is a 50 to 100 per cent error on every gain.
5. **Foreign-currency parcels** — Australian CPI applies regardless of instrument currency.
   Should work; warrants a test.
6. **New residential dwellings / affordable housing** (s115-102, s115-125) — irrelevant to a
   share tracker. Document as out of scope with a guard.

---

## Implementation status

Phases 0 to 5 of the revised plan are implemented and under test (275 tests). What is built:

| Area | Where |
|---|---|
| Fact table, discount, classification, TAP, residency, indexation, cutover, schedule | `share_dinkum_app/cgt/` |
| `ResidencyPeriod`, `Account.taxpayer_type`, `InstrumentValuation`, `CPIIndex`, `CapitalLossCarryForward`, `AttributionStatement`/`AttributionComponent`, `CGTReturnSnapshot` | `models.py`, migrations 0015–0020 |
| `CGTEventReport`, `CGTScheduleReport`, `CGTBasisChangeReport`, `BaseReport` | `reports.py` |
| `load_cpi`, `capture_cutover_valuations`, `suggest_instrument_classification` | `management/commands/` |

Still open, and each flagged at the point of use rather than silently defaulted:

1. **s112-185 apportioning instrument** — not made. A straddling disposal needs a market
   valuation; without one the row is reported unsplit and says why. `CGT_2027_REGIME_ENABLED`
   stays `False` until the instrument is registered.
2. **s115-115(4) market value election** — needs a valuation as at 8 May 2012 the application
   cannot hold. The no-election outcome under s115-115(6) is what is returned.
3. **Residential categories and Subdivision 26-155 quarantining** — steps 3 and 4 of the
   s102-5 method statement. A share tracker never produces them; a guard warns if one appears.
4. **`minimum tax gap amount`** (s119-10(2)) — needs total taxable income. The s119-5 base is
   output; the gap is not.
5. **Attributed gains are not residency-apportioned** — s115-105 apportions over the days the
   *asset* was owned, and for an attributed gain the trust owned it. An annual statement does
   not disclose when the trust bought what it sold. Narrow in effect: a foreign resident's
   attributed gains on non-TAP assets are disregarded outright under s855-40(2), so what
   remains is TAP attributions to a foreign resident, where the full rate is applied and the
   schedule flags it.
