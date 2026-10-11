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
  foreign or temporary follows the s115-105 question above. Filling the history settles it.
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
- The basis change report keys rows by sell allocation, and both halves of a disposal split at
  the cutover share one, so the pre-cutover half drops out of the comparison
  (`reports.py`, `CGTBasisChangeReport`).
- Snapshot totals include disregarded gains, since the realised gains report has no
  disregarded flag.
- The post-cutover slice reports an indexation factor of 1.000 even when not indexed, where
  `NO_INDEXATION` was meant (`cgt/events.py`, `_split_events`).
- Losses on unclassified instruments have no row in the return layout (`reports.py`).
- The all-years schedule (`build(account, None)`) counts a recorded carry-forward twice: as a
  prior-year loss, and again through its own year's events. Nothing calls it with `None` yet.

#### Robustness

- Loading an old export into a live portfolio merges rather than replaces, reactivating
  parcels deactivated since. Rows with an `id` do not fall back to their natural key, so an
  export only loads into an empty database or its own portfolio.
- Deleting a FiscalYear leaves `calculated_fiscal_year` null on the rows that pointed at it.
- `refetch_price_history` names only the holdings the provider returned nothing for. One it has
  only part of the history for keeps its older prices adjusted, and is not named.
- The CGT schedule download builds `Content-Disposition` by hand, so a portfolio name with a
  quote in it breaks the filename (`dashboard.export_cgt_schedule_view`).

#### Tests

- Parallel runs (`--parallel auto`) crash with `cannot pickle 'traceback' object`: some test
  errors in a way the parallel runner cannot report. Serial runs pass.
- Not covered: franking credit totals, `FLAT_RATE` indexation and partnership taxpayers.

#### Cleanup

- Phase 3 step 6 of the holdings refactor: delete the creation signals, `_creation_handled` logic,
  `STRUCTURAL_FIELDS` for buys, most of `chronology_problem`, the repair helpers and `_save_lock`,
  once the replay writer has been in use for a while. `HOLDINGS_WRITER=signals` goes with them.

#### Performance

- Creating a buy saves its instrument about six times.
- Loading an import template rebuilds each trade's instrument as it loads (about 28 s for the
  10-year sample). Rebuilding once per instrument at the end needs allocations that do not name a
  parcel as they load.
- `repair_portfolio_data` and a restore recalculate every record in the portfolio; fine at a
  few thousand records.
