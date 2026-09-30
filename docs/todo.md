### TODO

Left over from the September 2026 review of the signal and processing logic. Everything
fixed then is in the 0.3.0 changelog; this is what was not.

#### Waiting on advice

- **Does a discount apportioned under s115-105 and s115-115 survive 1 July 2027?** Act No. 49
  of 2026 leaves those sections unamended, but new s115-100(f) sets 0% where no other
  paragraph applies. The app applies the apportioned discount and the CGT schedule warns
  (`cgt/schedule.py`, `_s115_105_applies`). If the answer is 0%, `discount_percentage` should
  return 0 for events from 1 July 2027 where s115-105 applies.

#### Tax figures

- **An asset bought after 1 July 2027 keeps the old 50% when indexation is unavailable** to a
  resident individual or trust. Under s115-100(aa) and (f) it should get no discount. It only
  happens when residency is undeclared for part of the time (indexation needs every day), and
  the history's gaps are now reported, but the figure stays wrong until they are filled
  (`cgt/events.py`, `_single_post_cutover_event`).
- **Not implemented, and not flagged on the schedule:** CGT event I1 on departure without an
  s104-165(2) election, the s855-45 market value cost base on becoming a resident, and the
  pre-CGT exemption. Valuations for the first two can be recorded, but nothing reads them.
- **Trust gains have no loss-order category from 1 July 2027**, so they absorb losses after
  every categorised gain (`cgt/events.py`, `_attribution_events`).
- **Trust attribution events use the instrument's currency**, and component currencies are not
  checked, so a statement in another currency would mix currencies in the schedule.
- **A zero cost base is returned in AUD whatever the account currency** (`add_currencies`), so a
  zero-cost parcel in a non-AUD account fails on a currency mismatch.
- **A trade priced in one currency with brokerage in another fails an assert.**
  `attach_exchange_rate` converts from the currency of the first amount that is not zero, which
  is the price, and the brokerage then fails the currency check in `apply`.

#### Reports

- The basis change report keys rows by sell allocation, and both halves of a disposal split at
  the cutover share one, so the pre-cutover half drops out of the comparison
  (`reports.py`, `CGTBasisChangeReport`).
- Snapshot totals include disregarded gains, since the realised gains report has no
  disregarded flag.
- The post-cutover slice reports an indexation factor of 1.000 even when not indexed, where
  `NO_INDEXATION` was meant.
- Losses on unclassified instruments have no row in the return layout (`reports.py`).
- The all-years schedule (`build(account, None)`) counts a recorded carry-forward twice: as a
  prior-year loss, and again through its own year's events. Nothing calls it with `None` yet.
- Rounding: cost base components are rounded separately and can miss the cost base by 0.0001.

#### Robustness

- Loading an old export into a live portfolio merges rather than replaces, reactivating
  parcels deactivated since. Rows with an `id` do not fall back to their natural key, so an
  export only loads into an empty database or its own portfolio.
- Deleting a FiscalYear leaves `calculated_fiscal_year` null on the rows that pointed at it.
- `refetch_price_history` names only the holdings the provider returned nothing for. One it has
  only part of the history for keeps its older prices adjusted, and is not named.
- `devserver.py` backs up `db.sqlite3` before an update whatever `.env` says `DB_NAME` is, so
  with another name `uv run update` skips the backup and says there is no data.
- The CGT schedule download builds `Content-Disposition` by hand, so a portfolio name with a
  quote in it breaks the filename (`dashboard.export_cgt_schedule_view`).

#### Cleanup

- `ShareSplit.calculated_affected_parcels` never fills: the property is `affected_parcel_list`.
- `utils/signal_helpers.disconnect_app_signals` is broken on Django 6 and unused, and
  `DataLoader.get_or_create_exchange_rate` is dead code.
- `excelinterface.ExcelGen.add_table` discards its NaN clean-up (harmless: openpyxl writes NaN as
  a blank), and its default `template_info` names an unrelated Azure DevOps repository.

#### Performance

- `persist_safe_properties` evaluates every attribute in `dir()`, not just safe properties, and
  runs `model_to_dict` for its debug log on every save.
- Creating a buy saves its instrument about six times.
- The price refresh makes its network calls inside the admin's transaction.
- `repair_portfolio_data` and a restore recalculate every record in the portfolio; fine at a
  few thousand records.
