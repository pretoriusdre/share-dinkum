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

- **`uv run dev refetch_price_history`** fetches every stored price again, as traded (see below).
  It deletes nothing, and names any holding the provider no longer has, whose prices stay adjusted.

- **A CGT schedule warns when an earlier year's net capital loss is not recorded** as carried
  forward. Nothing is carried forward until it is, so the later year's net gain may be overstated.

- **The dashboard warns about a parcel whose cost base has gone below zero** from AMIT decreases.
  The excess is a capital gain in the year it arose (CGT event E10), which is not worked out here.

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

- **Records other records were worked out from cannot be changed after they are entered**: a buy
  or sell's instrument, date, quantity or currencies, a sell's strategy, a split's ratio, an
  adjustment's amount, an allocation's parcel or quantity. Previously the change was accepted and
  silently ignored by the parcels. Delete the record and enter it again. Price, brokerage and
  exchange rate corrections are still allowed, and now reach the parcels and gains.

- **A trade or split entered out of date order is refused**: a buy or sale dated before a split
  already applied, or a split dated before a sale already allocated.

- **Import templates load sales, splits and adjustments in date order**, each sale followed by its
  own sell allocations. Previously every sale was allocated before any split loaded, in row order.

- **A share split is dated on its ex-date.** A buy on that day is already in post-split units and
  is no longer split, as a sale that day already was not. A split entered after a sale on its own
  date is refused, like one entered after a later sale.

- **Prices are stored as traded that day**, not adjusted for the dividends and splits since. Every
  use of a stored price wants what a unit was worth on the day: the 30 June 2027 value was
  understated by any distribution going ex on 1 July, and the value chart paired adjusted prices
  with actual holdings. Stored prices change only when fetched again, so run
  `uv run dev refetch_price_history` once. A day fetched again now replaces the stored one, so a
  price taken while the market was open no longer stays as that day's close.

- **AMIT adjustments weigh every parcel in the same units.** A parcel sold before a share split in
  the same year, or one still held when an adjustment is entered after a later split, counted in
  different units from the rest. Existing adjustments are not reallocated.

- **A day with no exchange rate quoted (a weekend, a holiday) takes the last rate before it**, not
  the next one after.

- **Quantities must be more than zero** on buys, sales and splits, and a dividend's cannot be
  negative. Zero used to fail deep inside a save with a division by zero.

- **Loading a file again**:
  - A blank cell leaves the stored value alone, rather than clearing it. A blank `file` used to
    delete the attached document.
  - A transaction row with no `legacy_id` is refused once the portfolio has rows of that kind, since
    it would be added a second time. The same `legacy_id` on two rows of a sheet is refused.
  - A strategy or other choice may be given by its label or in any case (`fifo`); anything else is
    refused rather than stored as typed.
  - A blank trade currency is the instrument's, not AUD. A numeric `legacy_id` no longer gains `.0`.
  - An export's own DataExport rows are not loaded, and an export holds only its own portfolio.

### Fixed

- **Foreign-currency trades get their exchange rate before anything is built from them.**
  Previously the rate was attached last, which led to the cost base on foreign trades not being converted
  to the account's base currency.

- **Run `uv run dev repair_portfolio_data` if the dashboard says so.** It recalculates stored figures,
  carries adjustments a share split left behind to the parcels that replaced them, fetches exchange
  rates that could not be fetched, and lists what needs fixing by hand. `--dry-run` only reports.

- **A share split keeps the AMIT adjustments already allocated to a parcel.** They were left on the
  replaced parcel and dropped out of the cost base.

- **Deleting a share split reverses only the parcels it created**, and is refused once any of them
  has been sold or split again.

- **Sales with units allocated to no parcel are reported** on the CGT schedule and the dashboard.
  The gain on those units was in no report.

- **A parcel cannot be allocated to sales for more than it holds**, nor to a sale of another
  instrument or one dated before its purchase.

- **Parcels cannot be deleted in the admin.**


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

- **Exchange rates and CPI quarters can be saved in the admin**, which saved every record twice
  and rejected the second save's arguments on those. A rate corrected there stops being a stand-in,
  and what was converted at it is worked out again.

- **The admin no longer asks for an exchange rate on a foreign-currency sale.** One is attached on
  save, as for a buy.

- **Correcting a dividend's date or currency changes its exchange rate to match.** In a portfolio
  not held in AUD, amounts left at zero no longer attach an AUD rate to a dividend.

- **`capture_cutover_valuations --date` values the date given**, with `--purpose` to say what for.
  It valued 30 June 2027 whatever date it was given.

- **The dashboard charts follow share splits.** A holding stayed in pre-split units and went
  negative once post-split units were sold.

- **An offline update check is not retried for an hour**, rather than on every dashboard load.

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
