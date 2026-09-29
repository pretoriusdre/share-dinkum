# Changelog

All notable changes to Share Dinkum are recorded here. 

To upgrade, stop the server and run `uv run update`.

## 0.3.0

### Added

- **2027 CGT regime (preliminary).** Off by default; turn on **Model 2027 regime** in the account's
  tax settings. A holding bought before 1 July 2027 and sold after is split:
  - growth to 30 June 2027: CGT discount
  - growth from 1 July 2027: CPI indexation, no discount
  - not for anyone who has been a foreign or temporary resident (see residency below)
  - `uv run dev capture_cutover_valuations` records holding values at 30 June 2027.
  - `uv run dev load_cpi` loads the CPI series.

- **CGT schedule report.** Gains and losses by the instrument's legal structure (company, unit
  trust, stapled security, etc.). Losses are netted across the year and applied before the discount
  (s102-5).

- **Residency periods and TAP / NTAP.** Record residency history as `ResidencyPeriod` rows.
  - Always lived in Australia: declare one Australian residency period. No figures change.
  - Lived overseas: end the Australian period on departure, and record any s104-165(2) election to
    disregard the I1 deemed disposal.
  - History must be continuous back to your earliest purchase. Gaps are refused.
  - A foreign resident's gains on NTAP are disregarded (s855-10).
  - The CGT discount is apportioned for days as a foreign or temporary resident (s115-115).
  - If you were a foreign or temporary resident at any time after 8 May 2012 while holding an asset,
    it gets no 2027 split (s112-155(1)(d)). Instead:
    - abroad at any time from 1 July 2027 to the sale: apportioned discount, no indexation. The
      discount keeps shrinking while you remain abroad.
    - resident throughout from 1 July 2027: indexation from 1 July 2027 and no discount (s115-20).

- **Taxpayer type on the account**: individual, trust, company or complying super fund. A company
  gets no discount; a super fund gets one third. Until set, 50% is assumed and the dashboard says so.

- **MIT attribution statements** (`AttributionStatement`, `AttributionComponent`), from the annual
  tax statement.
  - Discounted gains are grossed up (trusts report them halved).
  - The TAP / NTAP split is kept, not netted.
  - Statements that do not reconcile are flagged.

- **Capital loss carry-forward tracking**, including an opening balance. This is needed by the CGT schedule.

- **CGT return snapshots** (`CGTReturnSnapshot`): a year's CGT figures at a point in time. The basis
  change report compares a snapshot with a fresh calculation. `uv run dev capture_cgt_snapshot` takes
  one for every year with a sale.

- **Instrument legal form and market country.**
  - `uv run dev suggest_instrument_classification` fills in what it can from dividend and
    distribution history and lists the rest.
  - Unclassified instruments are reported, not assumed.
  - A schedule stays a draft until each legal form is confirmed: tick **Confirm legal form** on the
    instrument, use the bulk action on the instrument list, or correct the suggestion. Saving an
    unrelated field does not confirm it.

- **Dashboard actions:** Refresh prices, Take capital gains snapshot, Export Australian CGT report,
  Export portfolio, Full backup.

- **`docs/capital_gains_models.md`**: what each new model is for and the order to fill them in.

### Changed

- **AMIT cost base adjustments are weighted by time held.** A parcel held for two months of a
  twelve-month year gets two months' share, not a full year's. Changes the cost base, and gains, of
  affected parcels. Existing adjustments are not reallocated; only new ones, or a load into a new
  portfolio.

- **The 12-month discount test is a calendar comparison**, not 365 days, so leap days no longer
  change the result. Also affects which parcels "minimise capital gain" selects.

- **Money is rounded to four decimal places where it is computed**, removing Decimal division
  artefacts (e.g. `2019.106200000001099999999996` for `2019.1062`). Snapshots are stored unrounded
  and compared at four places, so existing snapshots still match.

### Fixed

- **Foreign-currency trades get their exchange rate before anything is built from them.**
  Previously the rate was attached last, which led to the cost base on foreign trades not being converted
  to the account's base currency. If the dashboard warns about this, run `uv run dev repair_foreign_currency_figures`.
  That command recalculates the affected parcels, and lists any adjustments that need deleting and entering again.

- **Backups go to one place: `~/share-dinkum-backups/main/`**, for `uv run update`, the notebook's
  `backup()` and the dashboard. Previously two folders in two layouts, and only the notebook's was
  pruned.
  - The five most recent are kept.
  - Nothing existing is moved or deleted. Old-layout backups in `~/share-dinkum-backups/` are still
    found, and never pruned.

- **Refreshing prices no longer retries a delisted holding forever.** It stops a week after the sale,
  or after the sale was entered if that is later.

- **Proceeds and capital gain use one formula**, so proceeds less cost base equals the gain exactly.

- **Splitting a parcel or apportioning an adjustment no longer loses fractions of a cent.** The
  parts sum to the whole.

- **Reloading an import file no longer fails when a holding is fully sold** and the file includes
  its sell allocations.

- **An import that cannot resolve a parcel names the buy and the cause**: no buy with that legacy
  id, nothing left to allocate, or several candidate parcels. Previously a bare assertion.

- **Reimporting a portfolio no longer fails on a blank user email.** Blank cells in optional text
  columns load as empty strings. A missing date or quantity still fails.

## 0.2.0

Version 0.2.0 is the start of formal version tagging, although the tool has been under development for quite some time prior to this point.

### Added

- The dashboard shows the installed version and tells you when a newer release is available.
- `uv run dev make_import_template` generates an empty Excel import template from the models, so a
  new portfolio no longer starts by deleting sample rows out of a copy of the sample file.
- The import notebook takes a list of portfolios, so several Excel files can be loaded, each into its
  own portfolio.

### Changed

- A file is now loaded as a single transaction. A file that fails part way through, most often
  because a transaction names an instrument that was never listed, leaves nothing behind instead of a
  half loaded portfolio.
- Loading the same file again updates rows rather than duplicating them, matching on `legacy_id` for
  transactions and on the unique fields of reference tables such as Market and Instrument. Rows with
  no `legacy_id` are still always added, since two identical buys on one day are two real buys.
- A portfolio name must be unique per owner, as files are matched to portfolios by name.
- `clear_all_data` has moved to the bottom of the import notebook, commented out, under **Danger
  zone**. It deletes every portfolio and was never part of importing, though the README implied it
  was.

### Fixed

- Loading a data export into a *different* portfolio silently moved its records out of the original
  one instead of copying them. This is now refused.
- Updating an existing record during a load saved it twice, running every signal twice.
