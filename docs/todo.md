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
- **AMIT adjustment weights mix pre-split and post-split units** when an adjustment is entered
  after a later share split: held parcels count in post-split units, parcels sold before the
  split in pre-split units (`signals.py`, `allocate_cost_base_adjustment_now`).
- **Trust gains have no loss-order category from 1 July 2027**, so they absorb losses after
  every categorised gain (`cgt/events.py`, `_attribution_events`).
- **Trust attribution events use the instrument's currency**, and component currencies are not
  checked, so a statement in another currency would mix currencies in the schedule.
- **A zero cost base is returned in AUD whatever the account currency** (`add_currencies`), so a
  zero-cost parcel in a non-AUD account fails on a currency mismatch.
- **A trade priced in one currency with brokerage in another fails an assert.**
  `attach_exchange_rate` takes the first `*_currency` field alphabetically, which is brokerage.

#### Reports

- The basis change report keys rows by sell allocation, and both halves of a disposal split at
  the cutover share one, so the pre-cutover half drops out of the comparison
  (`reports.py`, `CGTBasisChangeReport`).
- Snapshot totals include disregarded gains, since the realised gains report has no
  disregarded flag.
- The post-cutover slice reports an indexation factor of 1.000 even when not indexed, where
  `NO_INDEXATION` was meant.
- Years with only trust statements are left out of the default CGT workbook, and losses on
  unclassified instruments have no row in the return layout (`reports.py`).
- Rounding: cost base components are rounded separately and can miss the cost base by 0.0001.

#### Robustness

- A zero quantity on a buy, sell or allocation, or a split with `quantity_before` of 0, divides
  by zero inside `persist_safe_properties`, which then aborts the save.
- Loading an old export into a live portfolio merges rather than replaces, reactivating
  parcels deactivated since. Rows with an `id` do not fall back to their natural key, so an
  export only loads into an empty database or its own portfolio.
- Deleting a FiscalYear leaves `calculated_fiscal_year` null on the rows that pointed at it.
- `Sell.clean` still makes the admin ask for an exchange rate on a cross-currency sale, although
  one is now attached automatically.

#### Cleanup

- The admin saves every object twice (`GenericModelAdmin.save_model`), and the `user=` it
  passes is discarded by `BaseModel.save`.
- `ShareSplit.calculated_affected_parcels` never fills: the property is `affected_parcel_list`.
- `utils/signal_helpers.disconnect_app_signals` is broken on Django 6 and unused, and
  `DataLoader.get_or_create_exchange_rate` is dead code.

#### Performance

- `persist_safe_properties` evaluates every attribute in `dir()`, not just safe properties, and
  runs `model_to_dict` for its debug log on every save.
- Creating a buy saves its instrument about six times.
- The price refresh makes its network calls inside the admin's transaction.
- `repair_portfolio_data` and a restore recalculate every record in the portfolio; fine at a
  few thousand records.
