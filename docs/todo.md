### TODO

Known gaps and work not yet done. What has been fixed is in `CHANGELOG.md`.

#### Waiting on advice

- **Does a discount apportioned under s115-105 and s115-115 survive 1 July 2027?** Act No. 49
  of 2026 leaves those sections unamended, but new s115-100(f) sets 0% where no other
  paragraph applies. The app applies the apportioned discount and the CGT schedule warns
  (`cgt/schedule.py`, `_s115_105_applies`). If the answer is 0%, `discount_percentage` should
  return 0 for events from 1 July 2027 where s115-105 applies.
- **Is an AMIT cost base increase indexed?** It depends on whether s114-15(2) reaches an
  increase to the total cost base. The app indexes it from the quarter it is made; set
  `CGT_INDEX_COST_BASE_INCREASES = False` in `constants.py` to add it at face value instead.
  Decreases are indexed either way (s114-15(3)).

#### Tax figures

- **A post-2027 acquisition with undeclared residency days keeps the pre-2027 treatment** (an
  apportioned discount, no indexation), flagged as a draft (`PENDING_UNDECLARED_INDEXATION`). The
  right figure depends on what the missing days were: resident gives indexation and no discount;
  foreign or temporary follows the s115-105 question above. Filling the history settles it. The
  post-2027 part of a sale split at 1 July 2027 is not indexed while such a gap exists, and is
  flagged the same way (`PENDING_UNDECLARED_SPLIT_INDEXATION`).
- **Not implemented, and not flagged on the schedule:** CGT event I1 on departure without an
  s104-165(2) election, the s855-45 market value cost base on becoming a resident, and the
  pre-CGT exemption. Valuations for the first two can be recorded, but nothing reads them, so
  someone who returns to Australia gets no warning.
- **Trust gains have no loss-order category from 1 July 2027** (waiting on which s102-6 category
  the Bill gives an attributed trust gain), so they absorb losses after
  every categorised gain (`cgt/events.py`, `_attribution_events`).

#### Reports

- **The income report (`income.py`) does not cover:**
  - interest (item 10), since no model records it;
  - a statement's own TFN withholding line, since it has no component. 13R is taken from
    distribution withholding instead, in the year paid, so a July payment's credit lands a year
    after the income it belongs to;
  - apportioning a statement for a mid-year residency change (it is flagged);
  - the LIC capital gain deduction (shown for reference, not computed);
  - layouts other than an individual's return.
- The all-years schedule (`build(account, None)`) counts a recorded carry-forward twice: as a
  prior-year loss, and again through its own year's events. Nothing calls it with `None` yet.

#### Source documents

- **Loading extracted events is not built** (`documents/`). When it is, round each per-unit figure
  to its field's decimal places before writing the import template, or the loader refuses the row.
  A rate printed in cents to 5 or 6 places, as NDQ's distribution advices do (`90.122396 cents`),
  comes out to 7 or 8 places in dollars, and the per-share fields hold 6. Rounding to 6 costs under
  half a cent per 10,000 units.
- **`Trade.unit_price` holds 4 decimal places**, but contract notes print the average price to 6
  (CommSec `1.234567`), so a cost base can be out by up to half of 0.0001 times the units.
  Widening it to 6 (with `max_digits` 21) is a migration that changes no stored value.

#### Robustness

- Loading an old export into a live portfolio merges rather than replaces, reactivating
  parcels deactivated since. Rows with an `id` do not fall back to their natural key, so an
  export only loads into an empty database or its own portfolio.
- Deleting a FiscalYear leaves `calculated_fiscal_year` null on the rows that pointed at it.
- `refetch_price_history` names only the holdings the provider returned nothing for. One it has
  only part of the history for keeps its older prices adjusted, and is not named.

#### Tests

- Parallel runs (`--parallel auto`) crash with `cannot pickle 'traceback' object`: some test
  errors in a way the parallel runner cannot report. Serial runs pass.
- Not covered: franking credit totals, `FLAT_RATE` indexation and partnership taxpayers.

#### Cleanup

- Phase 3 step 6 of the holdings refactor: delete the creation signals, `_creation_handled` logic,
  `STRUCTURAL_FIELDS` for buys, most of `chronology_problem`, the repair helpers and `_save_lock`,
  once the replay writer has been in use for a while. `HOLDINGS_WRITER=signals` goes with them.

#### Refactoring

- `dashboard.py: prepare_dashboard_context` is 424 lines. Split it into one builder per dashboard
  section, each returning its own context dict.
- `models.py` is 3,100 lines and holds price and exchange-rate fetching (`update_price_history`,
  `update_exchange_rate_history`). Move the fetching into a service module.
- `excelinterface.py: add_table` (171 lines) and `loading.py` (`prepare_table`, `load_rows`, about
  90 lines each) do several jobs. Separate reading, validating and writing into smaller steps.
- `signals.py` has 24 receivers, and much of the business logic runs inside them. Finish moving the
  holdings work to explicit calls (`HOLDINGS_WRITER`) rather than adding more receivers.
- Long CGT and income functions: `cgt/schedule.py: _warnings` (161 lines), `income.py: build`
  (129), and `cgt/events.py` helpers of about 90 lines each. Refactor only with `FiguresDumpTests`
  in place, and leave the tax rules unchanged.
- `tests.py` is 11,700 lines in one file. Split it by module (cgt, holdings, loading, documents,
  dashboard).
- 28 broad `except Exception` blocks outside the tests. Narrow each to the exceptions it can raise.

#### Performance

- Creating a buy saves its instrument about six times.
- Loading an import template rebuilds each trade's instrument as it loads (about 28 s for the
  10-year sample). Rebuilding once per instrument at the end needs allocations that do not name a
  parcel as they load.
- `repair_portfolio_data` and a restore recalculate every record in the portfolio; fine at a
  few thousand records.
