# Options, rights and employee share schemes

Record options or rights as an instrument of their own, and record exercising them with an **Option
exercise**. It sells the options and buys the shares in one step, and the shares' cost base carries
what the options cost. This page says how to fill one in for each case.

It describes how to record things in the app, and is not tax advice. For an employee share scheme,
your employer's ESS statement and the ATO's guidance are the authority.

## Recording the options

1. If no exchange quotes them, as with most employee options, put them on a **Market** of their own
   with **Is exchange listed** unticked. A price refresh skips instruments on an unlisted market.
   Left ticked, the app asks the price provider for the option's name as a ticker, which with no
   suffix is a US code, and may store another company's prices. Listed options go on their exchange.
2. Add an **Instrument** for them with legal form **Right, option or entitlement**. An exercise of
   anything else is refused. A gain on the options themselves, if you sell them or they lapse, goes
   in *Other CGT assets and any other CGT events* on the CGT schedule.
3. Enter a **Buy** of the options at what they cost, dated when you received them. The sections
   below say what that cost is; for most employee options it is 0. Unlisted options are shown as
   worth nothing, since no provider quotes them.

## Exercising them

An **Option exercise** takes:

- the options, the shares received, and the date;
- the number of options exercised, and of shares received (blank is one for each option);
- the **exercise price** per option, and the **fees** paid to exercise. Don't put the exercise price
  in brokerage anywhere: it is part of the cost base already;
- the **cost base basis**: ROLLOVER (what the options cost plus the exercise price), MARKET_VALUE
  (a share's market value, for a deferred employee scheme) or START_UP (as ROLLOVER, for the
  start-up concession). MARKET_VALUE also takes the market value per share at the taxing point, and
  the **taxing point date** if that was after the exercise;
- which options it takes if they were bought at different times: first bought (FIFO) or last (LIFO).

Saving it writes a sale of the options at nothing and a buy of the shares, one for each buy of the
options it took from, so options bought at different prices give shares with different cost bases.
Those trades are read-only: change or delete the exercise instead. It is refused if fewer options
were held on the date than it exercises, and deleting it is refused once any of its shares are sold.

Exercising makes no capital gain or loss (s134-1(4)). What the options cost goes into the shares,
and the CGT report shows it beside each sale of them as `option_cost_base`.

## Employee options and rights

Your employer's ESS statement says whether your scheme is **taxed upfront** or **deferred**, and the
discount included in your income. That discount is employment income, reported at item 12 from the
statement. Share Dinkum does not work it out; what it needs is the cost base of your shares. Shares
granted as shares, with no option to exercise, are [further down](#shares-rather-than-options).

### Deferred scheme, taxed at exercise (the usual case)

For options you can't transfer, the deferred taxing point is when you exercise them, once the shares
are free of any risk of forfeiture and free to sell. If the shares are held in escrow, it is when
the restriction lifts. It is never more than 15 years after the grant (s83A-120).

Leaving the job the options came from is not a taxing point. It was one until 30 June 2022: the
change applies to every option or share whose taxing point had not come by then, whenever it was
granted (Schedule 10 to the Corporate Collective Investment Vehicle Framework and Other Measures
Act 2022). If you left a job on or before 30 June 2022 still holding its deferred options, that day
was their taxing point if nothing came first: see
[Deferred scheme, taxed before exercise](#deferred-scheme-taxed-before-exercise), valuing the options
on the day you left. Shares from options already exercised but still restricted then take that day
as their taxing point date.

Record the options at 0, and exercise them with basis **MARKET_VALUE** and the market value of a
share at the taxing point, from your ESS statement. The shares are treated as acquired then for their
market value (s83A-125); the difference from what you paid is the discount, already taxed as income.
The exercise shows that difference as **ESS discount**, to compare with the statement. If you took
the fees off the discount you reported as income, leave them out here, so they are not counted twice.

If the shares were held in escrow after the exercise, give the **taxing point date**, when the
restriction lifted, and the market value then. The shares count as acquired on that date for
capital gains, so the 12 months for the discount run from it, though your holding shows them from
the exercise. A market value in another currency is converted at that date's exchange rate.

### Sold within 30 days of the taxing point

If you sell the shares within 30 days after the taxing point, the taxing point moves to the sale and
the whole gain is ESS income (s83A-120(3)), not a capital gain (s130-80(1)). Record the exercise and
the sale as usual: the CGT report lists the sale as disregarded, with that reason, and leaves it out
of the schedule. Exercising and selling on the same day is this case.

### Deferred scheme, taxed before exercise

This is uncommon: options that vested and could be sold or transferred before you exercised them, or
options held for 15 years. The options are treated as acquired at their own market value at the
taxing point (s83A-125), shown on your ESS statement. Record them at that value, dated at the taxing
point, and exercise them with basis **ROLLOVER**.

### Taxed upfront

The discount was taxed in the year you received the options, and they are treated as acquired then
for their market value (s83A-30). Record them at that value, dated when you received them, and
exercise them with basis **ROLLOVER**.

### Start-up concession

The options cost what you paid for them, usually nothing (s83A-30(2)). Record them at that, dated
when you received them, and exercise them with basis **START_UP**.

The shares' cost base is as for ROLLOVER, but for the CGT discount they count as acquired when you
received the options (s115-30(1) item 9A), so a sale 12 months after the grant gets the discount
even if it is soon after the exercise. The CGT report shows that date as the purchase date. The
1 July 2027 split and indexation still count from the exercise.

### Options that lapse or are forfeited

Close them with a **Sell** at 0 on the date. Options in a deferred scheme cost nothing, so that makes
no loss. If an amount was included in your income for them, as in a scheme taxed upfront, and you
forfeit them or they lapse, including after you leave your job or choose not to exercise them,
Division 83A is treated as never having applied (s83A-310). That is an amendment to your income, not
a capital loss: change their buy to what you paid for them (usually 0) before entering the sale.

### Shares rather than options

Some plans grant shares outright: a general employee plan offering up to $1,000 of shares a year, a
salary sacrifice plan, or shares you forfeit if you leave before they vest. There is nothing to
exercise; record a **Buy** of the shares. Your ESS statement says whether they were taxed upfront or
are deferred, and the market value used.

**Taxed upfront.** The shares are treated as acquired when you received them, for their market value
then (s83A-30), whatever you paid and whether or not up to $1,000 of the discount was tax-free
(s83A-35). Enter the buy on that date, at the market value per share, with any fees as brokerage.

**Deferred.** These are shares at real risk of forfeiture, or up to $5,000 a year of salary
sacrificed shares (s83A-105). The taxing point is when they are no longer at risk and you are free
to sell them, or 15 years after you received them if that is sooner (s83A-115). As for options,
leaving the job has not been one since 30 June 2022; a departure on or before that date was. The
shares are treated as acquired at the taxing point, for their market value then (s83A-125). Enter
the buy on that date, at that market value. The app shows the shares from the taxing point, not from
when you received them; dividends paid before then are recorded as usual.

Tick **Is deferred ess** on that buy. If you sell the shares within 30 days after the taxing point,
the taxing point moves to the sale and the whole gain is ESS income (s83A-115(3)), not a capital
gain (s130-80(1)): the CGT report lists the sale as disregarded and leaves it out of the schedule.
Selling as soon as the shares vest is this case.

### Options sold rather than exercised

Employee options can rarely be sold. If yours could, and you sold deferred options before their
taxing point or within 30 days after it, the gain is ESS income, not a capital gain (s83A-120(3),
s130-80(1)). Close the options with a **Sell** at 0 on that date: they cost nothing, so it makes no
gain. The same applies if you exercised options taxed when they vested and sold the shares within
30 days of that: record the options at 0, close them with a Sell at 0, and record neither the
exercise nor the sale of the shares. Sold later, they are an ordinary sale.

## Listed options and rights issues

### Options you bought, or were granted outside an employee scheme

Record them at what you paid, with buys and sells as usual. A sale is a capital gain or loss. An
option that lapses unexercised is a **Sell** at 0 on its expiry date: a capital loss of what it cost.
Exercise them with basis **ROLLOVER** (s134-1).

### Rights issues

Rights issued to you because you hold the shares cost nothing. If you take them up, enter a **Buy**
of the new shares dated when you accepted the offer, at the offer price. That is their whole cost
base, and they count as acquired then (s130-40, s130-45(2)). There is no need to record the rights.

If you sell the rights instead, they count as acquired when you bought the shares they came with
(s130-45(1)). Record a **Buy** of the rights at 0 dated on that purchase, one for each parcel if the
shares came in several, then the **Sell**.

## After 1 July 2027

With **Model 2027 regime** on, options you hold on 30 June 2027 are deemed sold and rebought that
day, like any other asset (s112-155). Exercising them later realises that deemed gain or loss, in
the year you exercise (s112-160): the CGT report shows it as the options' `pre_cutover` slice. The
shares then carry the options' 1 July 2027 value instead of what they cost, indexed from 1 July
2027, and the exercise price is indexed from the quarter of the exercise (s114-15(5)).

That needs a value for the options on 30 June 2027. `uv run dev capture_cutover_valuations` lists
the unlisted ones it cannot value; enter those by hand as an **Instrument valuation**. Until there
is one, the figures are marked pending.

Options in a deferred scheme whose taxing point has not yet come are the exception: every CGT event
before it is disregarded (s130-80(1)), so exercising them with basis MARKET_VALUE gives no gain.
