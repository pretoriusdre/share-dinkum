"""Division 115: the CGT discount percentage.

Two independent questions decide what fraction of a gain is discounted away.

**Who is making the gain.** s115-10 allows the discount to individuals, complying
superannuation funds and trusts, and not to companies. s115-100 then sets the rate: half
for an individual or trust, a third for a complying superannuation fund.

**Where they were while they held the asset.** s115-105 withdraws part of an individual's
discount for the days they spent as a foreign or temporary resident after 8 May 2012, and
s115-115 works out what is left. s115-100(c) routes to that result, which is also the
paragraph that keeps the apportioned discount alive past 1 July 2027: paragraph (f)'s 0%
applies only "if none of the above paragraphs applies", and where s115-105 applies,
paragraph (c) does.

The apportionment is inert for anyone who has always been an Australian resident: resident
days equal total days, so 50% x total/total is 50%. It is only the periods abroad that move
a number, which is what makes it safe to compute unconditionally once residency is declared.
"""

from datetime import timedelta
from decimal import Decimal

from share_dinkum_app.choices import TaxpayerType
from share_dinkum_app.cgt import residency
from share_dinkum_app.constants import CGT_DISCOUNT_RATE

#: The statutory rate as an exact value. constants.CGT_DISCOUNT_RATE is a float, which is
#: harmless while it only ever halves a number, but stops being harmless once it multiplies
#: an apportionment fraction: two runs over the same data could then differ in cents and a
#: basis change report would report movement that is only rounding noise.
FULL_DISCOUNT_RATE = Decimal(str(CGT_DISCOUNT_RATE))

#: s115-100(b): a complying superannuation entity discounts a third, not a half.
SUPERANNUATION_DISCOUNT_RATE = Decimal(1) / Decimal(3)

NO_DISCOUNT = Decimal('0')

#: Apportionment is worked out to more places than the rate itself is quoted to, because it
#: multiplies a dollar amount afterwards. Rounding to whole percent first can move a large
#: gain by hundreds of dollars.
_APPORTIONMENT_PRECISION = Decimal('0.00000001')

#: Entities s115-10 allows the discount to, and the rate each gets before apportionment.
_RATE_BY_TAXPAYER_TYPE = {
    TaxpayerType.INDIVIDUAL: FULL_DISCOUNT_RATE,
    TaxpayerType.TRUST: FULL_DISCOUNT_RATE,
    TaxpayerType.SMSF: SUPERANNUATION_DISCOUNT_RATE,
    TaxpayerType.COMPANY: NO_DISCOUNT,
    # A partnership is not a CGT entity: s106-5 makes each partner make their own capital
    # gain on their own share, so the discount is really the partners'. Half is the right
    # answer only if they are all individuals or trusts. The schedule report says so rather
    # than this module pretending to know.
    TaxpayerType.PARTNERSHIP: FULL_DISCOUNT_RATE,
    # Nothing declared. Keep what the application has always done, and let the report say
    # it is an assumption.
    TaxpayerType.UNDECLARED: FULL_DISCOUNT_RATE,
}

#: Only these have their discount apportioned by residency. s115-105 opens "you are an
#: individual"; s115-110 is its counterpart for trusts. A superannuation fund's rate is not
#: apportioned, and a company has nothing to apportion.
_APPORTIONED_TAXPAYER_TYPES = {
    TaxpayerType.INDIVIDUAL, TaxpayerType.TRUST,
    TaxpayerType.UNDECLARED, TaxpayerType.PARTNERSHIP,
}


def twelve_month_anniversary(purchase_date):
    """The same day of the month, one year on.

    29 February has no anniversary in a common year, so it falls back to 28 February --
    the convention the ATO uses for the 12-month rule.
    """
    try:
        return purchase_date.replace(year=purchase_date.year + 1)
    except ValueError:
        return purchase_date.replace(year=purchase_date.year + 1, day=28)


def is_discount_eligible(purchase_date, sale_date):
    """Whether a parcel has been held long enough for the discount.

    s115-25(1) requires the asset to have been "acquired ... at least 12 months before the
    CGT event". That is a calendar test, and this is what the application used to get
    wrong: counting 365 days made the outcome depend on whether a leap day happened to
    fall inside the holding period. An asset bought on 1 March and sold on the following
    1 March was treated as eligible when a leap year intervened, and identical holdings
    starting a year apart got different answers.

    Whether the anniversary *itself* qualifies is genuinely arguable. "At least 12 months
    before" reads as satisfied by exactly 12 months, but the ATO's guidance and common
    practice require the event to fall after the anniversary. The stricter reading is used
    here: it is the conservative one, since it can only ever deny a discount rather than
    claim one that is not available, and it preserves the application's existing intent.
    """
    if purchase_date is None or sale_date is None:
        return False
    return sale_date > twelve_month_anniversary(purchase_date)


def taxpayer_type_of(account):
    """The account's taxpayer type, or UNDECLARED where it is unset or unrecognised.

    An unrecognised value used to fall through a dict default straight to the full 50%.
    That is the quiet failure this module has to avoid: add a taxpayer type to the model
    and forget it here, and every gain that entity makes is silently halved, with no
    exception and no failing test. Treating an unknown as undeclared keeps the figure the
    same as it has always been *and* makes the schedule report say the assumption was made.
    """
    taxpayer_type = getattr(account, 'taxpayer_type', None)
    if taxpayer_type in TaxpayerType.values:
        return TaxpayerType(taxpayer_type)
    return TaxpayerType.UNDECLARED


def base_rate(account):
    """The discount rate before any residency apportionment."""
    return _RATE_BY_TAXPAYER_TYPE[taxpayer_type_of(account)]


def apportionment_fraction(account, purchase_date, sale_date, declared=None):
    """The s115-115 fraction of the discount that survives, between 0 and 1.

    Three cases, and the difference between them is worth spelling out because the obvious
    formula is only one of the three.

    **The holding began after 8 May 2012** -- s115-115(2). Every day is apportionable, so
    the fraction is simply the days of Australian residency over the whole holding.

    **The holding began earlier and the holder was an Australian resident on 8 May 2012**
    -- s115-115(3). The days before that date are not apportionable: the discount was not
    withdrawn retrospectively. So they count in the holder's favour whatever happened during
    them, and only absences afterwards reduce the fraction. For a long-held parcel this is a
    large difference, and treating it as case (2) would strip discount from years that
    Parliament deliberately left alone.

    **The holding began earlier and the holder was already abroad on 8 May 2012** --
    s115-115(6). Only Australian residency days after that date count towards the fraction.
    (s115-115(4) offers a market value election instead, which needs a valuation as at
    8 May 2012 that the application has no way to hold. It is not implemented, and what is
    returned here is the no-election outcome.)
    """
    if purchase_date is None or sale_date is None:
        return Decimal('1')

    total_days = (sale_date - purchase_date).days + 1
    if total_days <= 0:
        return Decimal('1')

    if declared is None:
        declared = residency.periods(account)

    cutoff = residency.APPORTIONMENT_START_DATE
    first_apportionable_day = cutoff + timedelta(days=1)

    if purchase_date > cutoff:
        counted = residency.resident_days(
            account, purchase_date, sale_date, declared=declared)
    elif residency.status_on(account, cutoff, declared=declared) == residency.RESIDENT:
        away = residency.non_resident_days(
            account, first_apportionable_day, sale_date, declared=declared)
        counted = total_days - away
    else:
        counted = residency.resident_days(
            account, first_apportionable_day, sale_date, declared=declared)

    counted = max(0, min(counted, total_days))
    fraction = Decimal(counted) / Decimal(total_days)
    return fraction.quantize(_APPORTIONMENT_PRECISION)


def discount_percentage(purchase_date, sale_date, account=None, declared=None):
    """The proportion of a capital gain that the discount removes.

    Returns a Decimal between 0 and 1: 0.5 means half the gain is discounted away.

    With no residency declared this is the flat rate for the taxpayer type, which for an
    undeclared account is the 50% the application has always applied. Declaring residency
    switches on the s115-105 apportionment -- and that only changes the answer if there were
    days abroad after 8 May 2012.
    """
    if not is_discount_eligible(purchase_date, sale_date):
        return NO_DISCOUNT

    rate = base_rate(account)
    if rate == NO_DISCOUNT:
        return NO_DISCOUNT

    if taxpayer_type_of(account) not in _APPORTIONED_TAXPAYER_TYPES:
        return rate

    if declared is None:
        declared = residency.periods(account)
    if not declared:
        return rate

    if not residency.has_non_resident_days_after_cutoff(
            account, purchase_date, sale_date, declared=declared):
        return rate

    return rate * apportionment_fraction(
        account, purchase_date, sale_date, declared=declared)


def apply_discount(amount, purchase_date, sale_date, account=None, declared=None):
    """Reduce a gain by the applicable discount percentage.

    A loss is returned untouched -- the discount only ever reduces a gain, and halving a
    loss would understate it.
    """
    if amount is None:
        return amount
    if getattr(amount, 'amount', amount) <= 0:
        return amount
    percentage = discount_percentage(
        purchase_date, sale_date, account=account, declared=declared)
    if percentage == NO_DISCOUNT:
        return amount
    return amount * (Decimal('1') - percentage)
