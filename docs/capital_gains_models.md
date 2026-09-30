# The capital gains models, and when you touch them

Eight new things appeared in the admin at once. Most of them you will never open. This is
what each one is for, grouped by how often you deal with it.

The organising idea behind all of them: **capital gains figures are never stored.** They are
worked out from your transactions every time you ask. That is what makes it safe to correct a
calculation — nothing already saved has to be rewritten. But a gain cannot be worked out from
transactions alone: it also depends on who you are, where you lived, and what kind of asset it
was. These models hold those facts. They are inputs, not results.

---

## Set once, then forget

### `Account.taxpayer_type`

Who owns the portfolio. An individual halves a capital gain, a complying superannuation fund
takes a third off, a company gets nothing at all.

It ships as **Not declared**, which behaves as an individual and says so on every report.
That is deliberate: guessing wrong here is a 50 to 100 per cent error on every gain you ever
make, so the application would rather ask.

**Do this once.** Account → your portfolio → Taxpayer type.

### `ResidencyPeriod`

Where you were an Australian tax resident, and when. One row per unbroken stretch, with an end
date on all but the last.

The CGT discount is not a flat 50%. Sections 115-105 and 115-115 reduce it in proportion to
the days you spent as a foreign or temporary resident after 8 May 2012. Without this the
application cannot work out the right discount for anyone who has lived abroad.

**If you have always lived in Australia, add one row — Resident, starting on or before your
earliest purchase, no end date — and every figure stays exactly as it is.** Resident days
equal total days, so the apportionment cancels out. It costs you nothing and it turns the
warning off.

If you have lived abroad, add a row per period. The history has to be continuous and reach
back to your first purchase: a gap is refused, because a parcel bought inside one has no
residency status and any status invented for it would silently decide a tax outcome.

`i1_election_made` only matters on a period where you *left* Australia. Tick it if you chose
under s104-165(2) not to pay tax on the deemed disposal at departure. Everything you held on
that day then stays inside the Australian tax net until you sell it, and anything you bought
afterwards does not — which is why it is recorded against a period and not against a holding.

---

## Copy off a document, roughly once a year

### `AttributionStatement` and `AttributionComponent`

The capital gains your ETFs and managed funds attributed to you, taken from the annual tax
statement (the AMMA statement) each issuer sends around August.

This is the big one for an ETF portfolio. A trust does not only pay you cash — it attributes
its own capital gains to you, on assets you never held, and those are yours for tax purposes.
The application previously had nowhere to put them, so a large part of the year's capital
gains was simply missing.

One `AttributionStatement` per fund per year. Then one `AttributionComponent` per line on the
statement — that is the long-and-narrow shape, so a new kind of component never needs a
schema change.

Two traps it handles for you:

- **Discounted gains are grossed up.** A trust reports them already halved, and you then apply
  your own discount. Carrying the statement's figure straight through would halve them twice.
- **Taxable and non-taxable Australian property are kept apart, not netted**, because that
  split is what decides whether a foreign resident can disregard the gain entirely.

Each statement is checked against itself: twice the discounted gains plus the other-method
gains must equal the total the statement states. Where it does not, the figures are flagged
rather than used.

### `Instrument.legal_form`

Whether a holding is a company, a unit trust, a stapled security. It decides which box a gain
goes in on the ATO capital gains schedule — AFI and VAS are both AUD on the ASX and land in
different boxes, so nothing can infer this from the market or the currency.

You do not set `legal_form_source` — it is not on the form. Setting the legal form
yourself records it as your answer, which is what lets a schedule stop calling itself a
draft. The command below marks its own guesses as suggestions.

    uv run dev suggest_instrument_classification --account "Your portfolio"

fills in what it can from your own dividend and distribution history — a holding that has paid
dividends is a company, one that has paid distributions is a trust — and lists what it cannot.
Anything left unclassified is reported as unclassified rather than guessed at.

### `CapitalLossCarryForward`

Capital losses available against a later year's gains.

Tick **is opening balance** for losses from returns you lodged before you started using this
application. Nothing in your transactions implies them, and without this every new user with
any history gets a wrong figure on their first schedule. Read it off your last notice of
assessment. Record it as a positive number.

---

## Before you change anything, or lodge anything

### `CGTReturnSnapshot`

A photograph of your capital gains figures for one year, on one day.

Because the figures are derived rather than stored, improving a calculation changes what the
application says about a year you may already have filed. A snapshot is the record of what it
used to say, so the change can be shown to you rather than quietly replacing a number on a
lodged return.

There is a **Take capital gains snapshot** button on the dashboard, or from a terminal:

    uv run dev capture_cgt_snapshot --account "Your portfolio" --lodged

Either captures every year that has a sale. The button never marks anything as lodged, since
the application cannot know what you filed; tick `is lodged` on the rows yourself, or use the
command flag.

Then, after you change something:

    CGTBasisChangeReport

recomputes the year and reports every line that moved, was added, or disappeared.

**You cannot type a snapshot in by hand.** The figures come from the report, and each
disposal is stored as its own `CGTReturnSnapshotRow`. A snapshot you can adjust afterwards is
not evidence of anything.

The allocation each row came from is kept as a plain identifier, not a link. A snapshot has
to outlive what it points at: a later sale can bifurcate a parcel and replace its
allocations, which is exactly the kind of change a snapshot exists to record.

Take one before: upgrading, declaring residency, setting your taxpayer type, classifying
instruments, or lodging.

---

## Nothing to do until 2027

### `CPIIndex`

Quarterly Consumer Price Index, from ABS 6401.0 series A2325846C. From 1 July 2027 the CGT
discount is replaced for individuals by indexing the cost base to inflation, and this is the
index. National data, so it belongs to no portfolio.

    uv run dev load_cpi cpi.csv

Where a quarter is missing, the application refuses to index rather than reaching for the
nearest quarter it has. A missing figure produces a visible gap; a guessed one produces a
plausible wrong number on a tax return.

### `InstrumentValuation`

What one unit of a holding was worth on a particular day.

Needed because 1 July 2027 works as a deemed sale: everything you hold is treated as sold at
market value that day and immediately rebought, which splits a long-held gain into a part that
keeps the old 50% discount and a part that gets indexation instead. Without a value for
30 June 2027, that split cannot be made.

    uv run dev capture_cutover_valuations

takes it from the prices already held and lists anything it cannot value, for you to enter by
hand. **Run it soon after the cutover.** The ordinary price refresh only covers instruments
with an open position or a recent sale, and a value for one specific day in 2027 cannot be
reconstructed later from a provider that has delisted the security.

Values are per unit, never per parcel, so a later share split does not corrupt them.

The prices it reads are stored as traded that day. Prices stored before 0.3.0 were adjusted for
the dividends and splits since, which understates a 30 June value by any distribution going ex on
1 July, so run `uv run dev refetch_price_history` once before capturing.

To value some other day, give it: `--date 2021-07-01 --purpose DEPARTURE`.

The same model covers three other deemed disposals that work identically and differ only in
what happens to the gain: leaving Australia (s104-165), arriving (s855-45), and pre-CGT assets
(s112-175).

---

## The order to do it in

1. Press **Take capital gains snapshot** on the dashboard — record where you stand
2. Set `Account.taxpayer_type`
3. Add your `ResidencyPeriod` rows
4. `uv run dev suggest_instrument_classification --account "..."`, then fill in what it could
   not work out
5. Enter any `CapitalLossCarryForward` opening balance
6. Enter your `AttributionStatement` rows from the annual tax statements
7. Look at `CGTBasisChangeReport` to see what moved, and `CGTScheduleReport` for the year

`CGTScheduleReport` refuses to call itself final while anything is unconfirmed — an
unclassified instrument, an undeclared residency, a statement that does not reconcile — and
tells you which. A draft schedule is not a broken one, but it must not be mistaken for a
finished one.
