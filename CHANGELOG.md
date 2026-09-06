# Changelog

All notable changes to Share Dinkum are recorded here. 

To upgrade, stop the server and run `uv run update`.

## Unreleased

### Added

- **Preliminary support for the 2027 Australian capital gains tax changes.** This is a major,
  complex feature. From 1 July 2027 an individual no longer receives a 50% gain discount. The cost
  base grows with inflation instead, so only the real gain is taxed, and a holding you have had
  since before that date is split in two: the growth up to the cutover keeps the old 50% discount,
  and the growth after it is indexed. It ships off: tick **Model 2027 regime** on the portfolio to
  turn it on, because the CPI figures the indexation needs do not exist yet for any quarter after
  the cutover. A year holding a disposal after that date says which of the two it was worked out
  under, and how to change it.

  Splitting a straddling gain is not the obstacle for a portfolio of listed holdings. The split uses
  market value at 1 July 2027, which for anything listed is the closing price, and
  `capture_cutover_valuations` already takes it. The apportioning method the Minister has yet to
  make is the alternative for real property and assets with no readily ascertainable market value —
  it was released in draft on 4 August 2026 as the *Income Tax Assessment (Method for Apportioning
  Capital Gains and Capital Losses) Determination 2026* and had not been registered as at
  6 September 2026. If you hold something that is delisted before the cutover, that is the case it
  matters for, and `capture_cutover_valuations` lists what it cannot value.

  Two things to do before that date, neither of which happens by itself: `uv run dev
  capture_cutover_valuations` records what your holdings were worth on 30 June 2027, which cannot be
  reconstructed later from a provider that has delisted the security, and `uv run dev load_cpi`
  loads the CPI series the indexation needs.

  **That is the headline case, and it is not everyone's.** If you have spent any time as a foreign
  or temporary resident since 8 May 2012, none of the paragraph above describes you. There is no
  deemed disposal at the cutover, so nothing is banked at the old rate (s112-155(1)(d), which is
  denied wherever the s115-105 apportionment applies). There is no indexation either, while you
  remain abroad: s114-25 asks whether you were a foreign or temporary resident at any point between
  1 July 2027 and the sale, and one week fails it as completely as ten years. What you keep is the
  apportioned discount, which s115-100(c) carries past the cutover unchanged -- paragraph (f)'s 0%
  applies only "if none of the above paragraphs applies", and here (c) does.

  The practical consequence is worth stating plainly, because it is easy to read this as a
  reprieve. The apportionment is resident days over total days, and once you are abroad the
  numerator stops growing while the denominator does not. Your discount therefore keeps shrinking
  for as long as you hold the asset, with no floor and no cutover to end it. A holding at 18% today
  is at 10% in ten years, on the same facts.

  Returning to Australia is not a clean escape from that either -- it banks nothing, and indexation
  then applies only to growth after 1 July 2027 while s115-20 denies the discount on an indexed cost
  base. `CGTEventReport` reports which of these cases each disposal falls in rather than assuming
  the headline one.

- **CGT schedule report.** A detailed capital gains schedule with gains and losses categorised by
  what the instrument legally is (company, unit trust, stapled security, and so on). Losses are
  netted across the year and applied before the discount, following the s102-5 method statement,
  and what carries forward is reported. The schedule marks itself a draft while anything is
  unconfirmed -- an unclassified instrument, an undeclared residency, a missing valuation, or a
  trust statement that does not reconcile -- and says which.

- **Support for Australian non-residency periods, and TAP / NTAP.** Record your residency history
  as `ResidencyPeriod` rows, including an s104-165(2) deemed disposal (I1) election on departure. A
  foreign resident's gain on non-taxable Australian property is reported as disregarded under
  s855-10 rather than taxed, and the CGT discount is apportioned for time spent abroad under
  s115-115. History has to be continuous and reach back to your earliest purchase; a gap is refused
  rather than filled in. If you have always lived in Australia, declaring one period of Australian
  residency reproduces every figure you have now, exactly.

- **Portfolio type on the account** -- individual, trust, company, or complying superannuation fund.
  A company gets no discount at all and a superannuation fund gets a third. Until you say which, the
  flat 50% is still applied and the dashboard says it is an assumption.

- **Capital gains attributed by managed investment trusts**, recorded as `AttributionStatement` and
  `AttributionComponent` from the annual tax statement. Discounted gains are grossed up, since a
  trust reports them already halved, and the taxable / non-taxable Australian property split is kept
  rather than netted. Each statement is checked against itself and flagged where it does not
  reconcile.

- **Tracking of CGT carry forward losses**, which the schedule needs, including an opening balance
  for losses made before you started using this application.

- **CGT return snapshots.** `CGTReturnSnapshot` records the capital gains figures for a fiscal year
  at a point in time, and the basis change report compares a snapshot against a fresh calculation.
  `uv run dev capture_cgt_snapshot` takes one for every year that has a sale.

- **`CGTEventReport`** lists every capital gains event with its full characterisation: the discount
  and how it was arrived at, whether the gain is disregarded and under which provision, the
  indexation factor, and which side of the cutover it falls. The existing realised capital gains
  report is unchanged, so a spreadsheet you have been exporting for years does not change shape.

- **Instrument legal form and market country.** `uv run dev suggest_instrument_classification` fills
  in what it can from your own dividend and distribution history and lists what it cannot. Anything
  still unclassified is reported as such rather than assumed.

  A suggestion is not an answer, and a schedule stays a draft until you confirm it. Correcting a
  suggestion confirms it. Agreeing with one needs saying so, which is a **Confirm legal form** tick
  on the instrument, or the same as a bulk action on the instrument list for a back catalogue of
  closed positions. It is deliberately not inferred from an ordinary save, so that editing an
  unrelated field cannot quietly answer a tax question for you.

- **Export portfolio button** on the dashboard. Previously you had to create a data export, then
  download the file from that.

- **Refresh prices button** on the dashboard. Previously you had to tick a box on the account
  object and save to update price history.

- **`docs/capital_gains_models.md`** explains what each of the new models is for and the order to
  fill them in. Most of them you never touch.

### Changed

These change how capital gains are worked out. They take effect on data you enter after upgrading,
and on anything you load into a new portfolio. They do not reach records that already exist, because
a cost base adjustment is spread across parcels once, when it is created.

- **Cost base adjustments are now weighted by the time a parcel was actually held.** A parcel bought
  part way through the year previously received a full year's share of an AMIT cost base adjustment,
  the same per unit as one held throughout. A parcel held for two months of a twelve month year now
  receives two months' worth. This changes the cost base of affected parcels, and every capital gain
  derived from them.

- **The 12-month holding test is now a calendar comparison, not a count of 365 days.** The old count
  made the answer depend on whether a leap day fell inside the holding period: an asset bought on
  1 March and sold the following 1 March qualified for the discount in one year and not in another.
  This can change which parcels the "minimise capital gain" sale strategy selects.

- **Reported money is rounded to four decimal places, where it is computed.** Decimal division is
  inexact, so selling 135 units out of 745 gave proceeds of 7154.835632530120481927710843, and
  fourteen of those summed to 2019.106200000001099999999996 instead of 2019.1062. Snapshots are
  stored unrounded, and the basis change report now compares at the same four places, so existing
  snapshots still show as unchanged.

### Fixed

- **Refreshing prices no longer chases a delisted holding forever.** It kept fetching a sold
  instrument until a price appeared after the sale, which never happens once a security stops being
  quoted, so a takeover or an expired rights entitlement was re-requested on every refresh. It now
  gives up a week after the sale is recorded — counted from when you enter it, so a disposal typed
  in months late is still looked up.

- **Proceeds and capital gain were computed by two different routes**, one dividing before
  multiplying and the other after, so the same quantity had two answers differing in the last digit.
  They now use one formula, and proceeds less cost base equals the reported gain exactly.

- **Splitting a parcel or apportioning an adjustment no longer loses fractions of a cent.** Where an
  adjustment was spread across parcels, or a parcel was bifurcated by a partial sale, the parts
  could sum to slightly less than the whole, out at the sixth decimal place. They now reconcile
  exactly.

- **Loading a data import file twice no longer fails when a holding has been completely sold.** A
  file can pin its own sell allocations, naming the buy each one came from. The parcel it names was
  resolved before checking whether the allocation already existed, and that lookup only accepts a
  parcel with quantity still available -- which on a second load there is none of. A partial sale
  survived it, because the unsold remnant still has quantity available.

- **An import that cannot find a parcel now says which buy and why.** It raised a bare assertion
  with no message, and the log line meant to print the offending row was itself malformed, so
  logging raised and buried the original error. Three causes are now named: no buy with that legacy
  id, nothing left of it to allocate, or several parcels available and no way to tell which was
  meant.

- **Reimporting a portfolio no longer fails when the user's email is blank.** Excel cannot tell an
  empty string from an empty cell and the loader turned every blank into a null, which the column
  would not accept. Since a data export is both the backup and the only migration path off this
  application, that made the backup silently not one. Blank cells in text columns that cannot hold a
  null now load as empty; a missing date or quantity still fails.

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
