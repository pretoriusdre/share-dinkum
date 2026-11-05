# Options, rights and employee share schemes

Share Dinkum has no record yet for exercising an option, so options cannot turn into shares inside
the app ([issue #41](https://github.com/pretoriusdre/share-dinkum/issues/41)). Until they can,
record the **shares** you end up with, at the cost base the law gives them. Record the options
themselves only where they matter on their own: when you sell them, or they lapse.

This page says what to enter in each case. It describes how to record things in the app, and is not
tax advice. For an employee share scheme, your employer's ESS statement and the ATO's guidance are
the authority.

## What the legal form does

**Right, option or entitlement** on an instrument only decides where a gain on that instrument goes
on the CGT schedule: *Other CGT assets and any other CGT events*. It does not exercise anything.
Shares you get by exercising an option go in the category for the company's shares, like any other
shares.

## Don't put the exercise price in brokerage

Brokerage is part of the cost base of the instrument the buy is for. On an option instrument it
stays with the options and never reaches the shares you later sell. On the shares it usually gives
the wrong cost base anyway (see below). Keep brokerage to the fees you actually paid.

## Employee options and rights

Your employer's ESS statement says whether your scheme is **taxed upfront** or **deferred**, and the
discount included in your income. That discount is employment income, reported at item 12 from the
statement. Share Dinkum does not work it out; what it needs is the cost base of your shares.

### Deferred scheme, taxed at exercise (the usual case)

For options you can't transfer, the deferred taxing point is when you exercise them, once the shares
are free of any risk of forfeiture and free to sell. If the shares are held in escrow, it is when
the restriction lifts. It is never more than 15 years after the grant (s83A-120).

Enter a **Buy** of the shares:

- **date:** the taxing point. The app shows the shares from then, not from the exercise.
- **unit price:** the market value of a share at that time, as on your ESS statement.
- **brokerage:** the fees you paid to exercise or to receive the shares, not the exercise price.
  If you took those fees off the discount you reported as income, leave them out here, so they
  are not counted twice.

Don't record the options. The shares are treated as acquired at the taxing point for their market
value (s83A-125), so neither what you paid for the options nor the exercise price is part of their
cost base: the difference is the discount, already taxed as income.

### Sold within 30 days of the taxing point

If you sell the shares within 30 days after the taxing point, the taxing point moves to the sale
and the whole gain is ESS income (s83A-120(3)). There is no capital gain, so **record neither the
buy nor the sale**. Exercising and selling on the same day is this case.

### Deferred scheme, taxed before exercise

This is uncommon: options that vested and could be sold or transferred before you exercised them, or
options held for 15 years. The options are treated as acquired at their own market value at the
taxing point (s83A-125), shown on your ESS statement. Enter a **Buy** of the shares dated when you
exercised, with unit price = (market value of the options at the taxing point + the exercise price
you paid) ÷ the shares you received (s134-1).

### Taxed upfront

The discount was taxed in the year you received the options, and they are treated as acquired then
for their market value (s83A-30). Enter a **Buy** of the shares dated when you exercised, with unit
price = (market value of the options when you received them + the exercise price you paid) ÷ the
shares you received.

### Start-up concession

The options cost what you paid for them, usually nothing (s83A-30(2)). Enter a **Buy** of the shares
dated when you exercised, with unit price = (what you paid for the options + the exercise price) ÷
the shares you received.

For the CGT discount, though, the shares count as acquired when you received the options
(s115-30(1) item 9A). The app counts from the buy date, so it gives no discount on a sale more than
12 months after the grant but within 12 months of the exercise. Check such a sale by hand.

### Options that lapse or are forfeited

If you never recorded the options, there is nothing to record. If an amount was included in your
income for them and you then forfeit them or they lapse, including after you leave your job or
choose not to exercise them, Division 83A is treated as never having applied (s83A-310). That is an
amendment to your income, not a capital loss.

### Listing the options in your portfolio

The tax figures don't need them. To list them anyway:

1. Put them on a **Market** of their own with **Is exchange listed** unticked. A price refresh skips
   instruments on an unlisted market. Left ticked, the app asks the price provider for the option's
   name as a ticker, which with no suffix is a US code, and may store another company's prices.
2. Set the instrument's legal form to **Right, option or entitlement**.
3. Enter a **Buy** at the cost the case above gives the options (usually 0), dated when you received
   them.
4. When you exercise, enter a **Sell** of the options on that date at the same unit price as their
   buy, with no brokerage, to close the holding with no gain or loss. It shows on the CGT report as
   a sale with no gain.

They are shown as worth nothing, since no provider quotes them.

## Listed options and rights issues

### Options you bought, or were granted outside an employee scheme

Record them as an instrument of their own, with legal form **Right, option or entitlement**, and buys
and sells as usual. A sale is a capital gain or loss. An option that lapses unexercised is a
**Sell** at 0 on its expiry date: a capital loss of what it cost.

When you exercise some, enter:

- a **Buy** of the shares dated when you exercised, with unit price = (what you paid for those
  options + the exercise price) ÷ the shares you received (s134-1); and
- a **Sell** of those options on the same date at their parcel's unit cost base, with no brokerage,
  so they leave the holding with no gain or loss.

### Rights issues

Rights issued to you because you hold the shares cost nothing. If you take them up, enter a **Buy**
of the new shares dated when you accepted the offer, at the offer price. That is their whole cost
base, and they count as acquired then (s130-40, s130-45(2)). There is no need to record the rights.

If you sell the rights instead, they count as acquired when you bought the shares they came with
(s130-45(1)). Record a **Buy** of the rights at 0 dated on that purchase, one for each parcel if the
shares came in several, then the **Sell**.

## After 1 July 2027

With **Model 2027 regime** on, options you hold on 30 June 2027 are deemed sold and rebought that
day, like any other asset (s112-155). Exercising them later realises that deemed gain or loss in
the year you exercise (s112-160), and the shares' cost base starts from the options' value on
1 July 2027 rather than what you paid. Employee options in a deferred scheme whose taxing point has
not yet come are the exception (s130-80(1)). The app does not handle this yet, so work out that
year's figures by hand.
