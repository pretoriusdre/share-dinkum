# Changelog

All notable changes to Share Dinkum are recorded here. 

To upgrade, stop the server and run `uv run update.py` (`uv run update` before 0.6.1).

## Unreleased

### Added

- **Choose the portfolio you are working on**, from the **Portfolio** list at the top of every page.
  Only the first portfolio you created could be shown before, so a second one loaded later never
  appeared on the dashboard. The dashboard, its reports and buttons, and the admin follow the
  portfolio you choose, and it is remembered. A portfolio you add in the admin is chosen for you.

- **Check a folder of documents against a portfolio (experimental).** `uv run dev check_documents
  --account NAME FOLDER` lists each payment, trade or statement line the portfolio holds
  differently or lacks; `--out` writes the findings to a workbook.
  - Added deterministic parsers for:
    - CommSec contract notes
    - SelfWealth contract notes
    - Computershare dividend advices, including dividend reinvestment advices
    - Vanguard ETF distribution advices
    - Link Market Services / MUFG Corporate Markets dividend and distribution advices
    - Advanced Share Registry dividend advices
    - Boardroom dividend statements
    - Computershare annual tax statements (Vanguard ETFs)
    - Link Market Services / MUFG Corporate Markets annual tax statements (BetaShares)
    - CHESS holding statements, each balance checked against the units the trades give that day
    - Interactive Brokers activity statements (CSV): trades, dividends and distributions, and holdings
  - `uv run dev parser_prompt FILE` writes a brief for adding a parser.
  - `--ai` reads an unrecognised file with a local or hosted AI model, redacted first.
    Check its findings against the document.
- **Redacted copies of your documents, made on your machine.** `uv run dev redact_documents
  FOLDER --out COPIES` writes a text copy of each document with your own list of names, addresses
  and other terms (`redact_terms.txt`) taken out, as well as TFNs, HINs, SRNs, bank details and
  email addresses, and points at any line that still looks personal. Share the copies, never the
  originals, when asking for a parser for your registry or broker.
- **Scanned PDFs are read by OCR, on your machine.** Install it with `uv sync --inexact --extra
  ocr`. `check_documents` and `redact_documents` can then read a scanned PDF. The `--out`
  workbook marks each scanned file, as OCR can misread a figure.

### Changed

- **The sample data lists only the 10 instruments it holds**, not the whole ASX list (2,352), so
  loading it no longer fills that portfolio's instrument list with ones it never trades.
- **The admin shows only the chosen portfolio.** Lists, record pages, actions and the pickers
  (eg the instrument on a buy) used to show every portfolio's records together. A new record goes
  into the chosen portfolio. Opening another portfolio's record says which portfolio it is in.
- **Attached documents and exports need a login.** They are served only to a user who may see
  their portfolio.
- **Only a superuser can change the shared tables** (fiscal years, fiscal year types and CPI), which
  every portfolio uses.
- **A buy's or sell's unit price holds six decimal places**, up from four, as contract notes print
  the average price to six (eg `1.234567`). Four places could put a cost base out by up to half a
  cent per 100 units. Prices already stored are unchanged; enter the full price from the contract
  note to correct one. The sale's proceeds per unit are kept to six places as well.
  `check_documents` now compares a trade's price to six places, so a price entered rounded shows
  as differing from its contract note.

### Removed

- **`migrate.bat`, `runback.bat` and `runtests.bat`.** Use `uv run dev` to start the app,
  `uv run dev migrate` to apply database changes and `uv run dev test share_dinkum_app` to run the
  tests. A shortcut to `runback.bat` should be pointed at `uv run dev` instead.

### Fixed

- **A record can no longer refer to another portfolio's record**, eg a buy of an instrument from
  another portfolio, which the admin allowed. The dashboard warns about any such record already
  stored, and `uv run dev repair_portfolio_data` lists them to delete and enter again.
- **`--account` on the commands refuses a name that two users' portfolios share**, rather than
  running on both. Give it as `owner/name`, or the portfolio's id.

## 0.6.1 - 2026-10-18

### Fixed

- **The update command is now `uv run update.py`.** On Windows, `uv run update` stopped at
  "Installing any new dependencies" with `failed to remove file ... Scripts/update.exe: Access is
  denied`, since installing the new version replaced the program running the update. The new command
  does not run from that file. `uv run update` now just tells you the new command.
  - The update to 0.6.1 itself still runs the old command, so on Windows it may fail this one last
    time. If it does, finish it with `uv sync`, then `uv run dev migrate`.

- **The importer handles irregular files.** Excel can rename a named table when you copy its
  sheet in from another workbook (eg `Instrument` becomes `Instrument18`). The importer used to skip
  such tables silently, which caused subsequent load errors. It now loads the table with a warning.
  Table names and column headers that differ only in case or spaces are also accepted.

- **Import errors are clearer.** If rows name an instrument (or other record) that isn't in the
  portfolio, one message lists every missing name, with its rows and the closest stored name. Names
  which differ only in case (eg bhp / BHP) are accepted with a warning.

- **A parcel held under 12 months is no longer indexed from 1 July 2027.** Indexation applies only to
  an asset acquired at least 12 months before the sale (s114-10(1)), but the report indexed any
  parcel the holder had been resident for, so it understated the gain. A shorter hold now gets no
  indexation and no discount, and the full gain is taxed. A sale on the 12-month anniversary counts as
  under 12 months, as it already does for the discount. The Unrealised sheet's `discount_eligible_from`
  is blank for a resident who will be indexed instead.

## 0.6.0 - 2026-10-17

### Added

- **Choose where exports, documents and backups are kept**, in `share_dinkum_proj/.env`:
  - `MEDIA_LOCATION`: the folder that holds the `media` folder of exports and attached documents.
    Default the project folder, so `share_dinkum_proj/media`.
  - `BACKUP_LOCATION`: the folder that holds the `share-dinkum-backups` folder of backups. Default your
    home folder, so `~/share-dinkum-backups`.
  - Existing files are not moved. Move the `media` or `share-dinkum-backups` folder yourself before
    changing either setting.
  - Only the `media` and `share-dinkum-backups` folders inside those locations are ever written to,
    pruned, copied, emptied or restored, so it is safe to name your Documents folder, your home folder
    or a drive. Clear-all and restore remove only the account folders in `media`, and the start-up
    check warns about any other files already in it.
  - An existing `.env` is not updated. Add the two lines to it if you want them.
- **Unrealised gains are now part of the Australian CGT report.** Each parcel you hold is treated as
  if you sold it today, so it shows the discount it would get and when it becomes
  eligible. From 1 July 2027 it is split and indexed as a real sale would be. It is a guide for
  planning a sale only; none of it goes on a return.
- **The Australian CGT report shows what has changed since your last capital gains snapshot**, on
  a new ChangesSinceSnapshot sheet. It compares each year's lodged snapshot, or else its latest.

### Changed

- **The portfolio export no longer has a RealisedCapitalGains sheet.** The Australian CGT report
  replaces it. That report is more complete: it applies the discount, losses, residency and the 2027
  changes.
- **Capital gains snapshots record the whole year**: its totals, including net capital gain, trust
  gains, both parts of a sale split at 1 July 2027, and how each gain is taxed. A change in the
  discount or residency now shows up, not only a change in the gain. Snapshots you took before
  this are compared on what they recorded.
- **Reporting date is configurable**, previously it was always today:
  `uv run dev --reporting-date 2026-06-30`. It lasts for that run only.
  It governs whether a fiscal year is still running, the residency check, the dashboard chart and
  the date a capital gains snapshot is taken. Unrealised gains are always as at today.
- **You no longer need to create `.env`.** The first start copies `.env.sample` and generates a
  `SECRET_KEY`.

## 0.5.0 - 2026-10-11

Release 0.5.0 changes the way that parcels are built from trades. Previously, each 'buy' trade
created a parcel at the moment of entry. Things which act on or attach to those parcels, like
sells, splits, and cost base allocations, used those existing parcels, so needed to be added
afterwards. But what if you made a mistake in one of your old buy entries? That could affect all
of the subsequent calculations in a way which is difficult to fix. This release solves that type
of scenario. Every time you add, delete or update a trade, everything which is derived from it is
recalculated, and the difference is applied. Upgrading does not change your existing figures.

### Added

- `uv run dev check_holdings` works each portfolio's parcels and allocations out again from its
  trades and lists any figure that differs from what is stored. `--rebuild` accepts the difference
  and rewrites them. `--respread` lists older cost base adjustments that today's spread would divide
  differently. The dashboard warns when there is a difference.
- **Reallocate** cost base adjustments: an admin action that spreads the selected adjustments again
  over the holding as it now stands, for example after entering a buy you had missed. It shows each
  buy's share before and after, and names the sales whose gain would change, before it changes
  anything.

### Changed

- **Parcels are recalculated from trades.** Saving or deleting a buy, sale, share split, cost base
  adjustment or allocation works the instrument's parcels out again from its trades, in date order,
  in the same save. So these are now accepted:
  - a buy or sale dated before a share split already entered;
  - a buy or sale dated before a cost base adjustment already spread. The spread is kept as it was,
    and the admin says so; Reallocate spreads it over the holding as it now stands;
  - a corrected date, quantity or instrument on a buy. A correction a sale already made cannot
    follow (fewer units than were sold, say) is refused.

  A share split dated before a sale already allocated is still refused. Lodged CGT snapshots still
  match.
- Deleting an allocation of a FIFO, LIFO or minimum-CGT sale makes the sale MANUAL, so it is not
  allocated again straight away.
- If an instrument's stored parcels give different figures from its trades, changes to it are
  refused until `uv run dev check_holdings --rebuild` accepts the difference.
- Each sale allocation records the buy it took units from, and exports carry it.
- Offline, a price or exchange rate that could not be fetched is not tried again for five minutes.
  Refresh prices always tries again.
- The portfolio's "Update price history" tick-box is removed. Use Refresh prices on the dashboard.
- `uv run update` now uses `DB_NAME` in `.env`, instead of hardcoded value `db.sqlite3`.

### Fixed

- A buy can now be deleted. Its parcels go with it, and its share of each cost base adjustment is
  reallocated to the buys still held. It is still refused if a sale took units from it (delete the
  sale first), or if it holds a share of an adjustment allocated manually (move or delete that
  allocation first).
- A managed fund's annual statement entered in a currency other than the portfolio's is now left
  out of the capital gains schedule and named, rather than counted wrongly.
- In a portfolio whose currency is not AUD, a zero amount (a parcel bought for nothing, or a
  dividend of nothing) was stored as AUD and could not be converted.
- With the 2027 changes modelled, the part of a sale after 1 July 2027 that is not indexed showed
  an indexation factor of 1.000, it is now blank. Where it is not indexed only because some
  residency days are undeclared, it is marked as not final with a warning.
- The CGT report's return layout had no line for a loss on an unclassified instrument, so
  the loss lines fell short of the total. Added the line.
- A portfolio name with a double quote in it broke the CGT and income report download's filename.

## 0.4.0 - 2026-10-04

A minor release focused on documentation, improved sample data, ease of importing, and reporting.
Support for the 2027 CGT changes was previously added in 0.3.0.

### Added

- **Australian income report.** A dashboard button exports dividends and trust income by return
  label (items 11, 13, 20) for every year, from each fund's annual statements. Income while a foreign
  resident is listed separately with the tax withheld; a year is a draft until statements are in and
  residency is declared.
- Annual statements gain a franking credit line (label 13Q).
- **Gain subject to MIT withholding** on annual statements. Tick it where you were a foreign
  resident and the fund withheld MIT withholding tax on the taxable Australian property part of a
  gain: the gain is then left out of the CGT report, and shown as disregarded in the Events sheet.
- Full backup also writes an Excel export of each portfolio into the backup folder.
- The import template now has optional tables for residency periods, instrument valuations, capital
  loss carry-forwards and managed fund annual statements (with their components). They are marked
  optional on the index sheet and can be left empty or entered in the app later.
- `uv run dev make_fake_data` rebuilds the sample file as a fake 10-year portfolio: monthly core ETF
  buys, USD satellites, small holdings, fully and partly sold positions, dividends and
  distributions from real per-share history, and annual statement data.
- `data_import.ipynb` portfolios accept `taxpayer_type` and `tax_settings_reviewed`, so a portfolio
  can start without the undeclared tax settings warning.
- Every column header in an export and the import template has a note on what the column is, whether
  it is required, and what a blank means.
- Columns of the import template that take a fixed set of values (sell strategy, dividend type,
  residency status, currency, and yes/no columns such as `i1_election_made`) now have a dropdown in Excel.

### Changed

- Loading a file now strips spaces around text cells, reads numeric stock codes
  (as on the Hong Kong exchange) as text, and refuses a table or sheet claiming more than 2,000,000 cells.
- Cost base adjustments after 1 July 2027 are indexed from the quarter they are made. Whether
  increases are indexed at all is still unclear in the current legislation, and could change in
  future releases.

### Fixed

- A blank cell in a yes/no import column that has a default (such as `is_exchange_listed`) failed the
  load with a NOT NULL error. It now takes the default.
- Fiscal year names are looked up within the portfolio's own fiscal year type, so another portfolio's
  year of the same name cannot be matched by mistake.
- Trades dated before an applied cost base adjustment are now refused. Delete the adjustment, add the
  trades, and apply it again.
- The dashboard now warns about a cost base adjustment for a year the instrument wasn't held.
- Trades with brokerage in a different currency from the price (such as AUD brokerage on a USD trade)
  can now be saved. Each amount is converted at its own currency's rate.
- A dividend's company tax of 100% or more is now refused.
- A blank `lookup_legacy_*` cell, such as an annual statement with no cost base adjustment, failed
  the load looking for a record called "nan".
- Fix slow performance issue on CGT report.

## 0.3.0 - 2026-09-30

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
  a snapshot for every year with a sale.

- **Instrument legal form and market country.**
  - `uv run dev suggest_instrument_classification` fills in what it can from dividend and
    distribution history and lists the rest.
  - Unclassified instruments are reported, rather than assumed. You'd need to check these.
  - A schedule stays a draft until each legal form is confirmed: tick **Confirm legal form** on the
    instrument, use the bulk action on the instrument list, or correct the suggestion.

- **Dashboard actions:** Refresh prices, Take capital gains snapshot, Export Australian CGT report,
  Export portfolio, Full backup.

- **`docs/capital_gains_models.md`**: what each new model is for and the order to fill them in.

- **`uv run dev refetch_price_history`** fetches every stored price again, as traded (see below).
  It deletes nothing, and names any holding the provider no longer has, whose prices stay adjusted.

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

- **The dashboard charts now compensate for share splits.** Previously, a holding stayed in pre-split 
  units and went negative once post-split units were sold.

- **An offline update check is not retried for an hour**, rather than on every dashboard load.

## 0.2.0 - 2026-08-28

Version 0.2.0 is the start of formal version tagging, although the tool has been under development since July 2025.

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
