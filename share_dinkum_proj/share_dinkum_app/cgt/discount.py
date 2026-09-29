"""Division 115: the CGT discount percentage.

* The rate depends on the taxpayer type (s115-10, s115-100): half for an individual or
  trust, a third for a complying super fund, none for a company.
* It is apportioned for days as a foreign or temporary resident after 8 May 2012
  (s115-105, s115-115). Days no residency period covers count as not resident too.
* From 1 July 2027, Act No. 49 of 2026 ends the 50% for individuals and trusts
  (s115-100(aa), (ab)) and adds s115-100(f), 0% where no other paragraph applies. Whether
  a discount apportioned under s115-105 and s115-115, both unamended, survives that is not
  settled. It is applied here, and the CGT schedule warns that it is unsettled.
"""

from datetime import timedelta
from decimal import Decimal

from share_dinkum_app.choices import TaxpayerType
from share_dinkum_app.cgt import residency
from share_dinkum_app.constants import CGT_DISCOUNT_RATE

#: `constants.CGT_DISCOUNT_RATE` as a Decimal, to avoid float error.
FULL_DISCOUNT_RATE = Decimal(str(CGT_DISCOUNT_RATE))

#: s115-100(b): a complying superannuation entity discounts a third, not a half.
SUPERANNUATION_DISCOUNT_RATE = Decimal(1) / Decimal(3)

NO_DISCOUNT = Decimal('0')

#: Precision of the apportionment fraction; high, because it multiplies dollar amounts.
_APPORTIONMENT_PRECISION = Decimal('0.00000001')

#: Discount rate by taxpayer type, before apportionment.
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

#: Taxpayer types whose discount is apportioned by residency (s115-105, s115-110 for trusts).
_APPORTIONED_TAXPAYER_TYPES = {
    TaxpayerType.INDIVIDUAL, TaxpayerType.TRUST,
    TaxpayerType.UNDECLARED, TaxpayerType.PARTNERSHIP,
}


def twelve_month_anniversary(purchase_date):
    """The same date one year on; 29 February becomes 28 February."""
    try:
        return purchase_date.replace(year=purchase_date.year + 1)
    except ValueError:
        return purchase_date.replace(year=purchase_date.year + 1, day=28)


def is_discount_eligible(purchase_date, sale_date):
    """Whether the sale is after the purchase's 12-month anniversary (s115-25(1)).

    A calendar test, not 365 days. A sale on the anniversary itself does not qualify, the
    conservative reading.
    """
    if purchase_date is None or sale_date is None:
        return False
    return sale_date > twelve_month_anniversary(purchase_date)


def taxpayer_type_of(account):
    """The account's taxpayer type, or UNDECLARED if unset or unrecognised, so reports flag it."""
    taxpayer_type = getattr(account, 'taxpayer_type', None)
    if taxpayer_type in TaxpayerType.values:
        return TaxpayerType(taxpayer_type)
    return TaxpayerType.UNDECLARED


def base_rate(account):
    """The discount rate before any residency apportionment."""
    return _RATE_BY_TAXPAYER_TYPE[taxpayer_type_of(account)]


def apportionment_fraction(account, purchase_date, sale_date, declared=None):
    """The s115-115 fraction of the discount kept, between 0 and 1. Counted days over total:

    * Bought after 8 May 2012 (s115-115(2)): resident days.
    * Bought earlier, resident on 8 May 2012 (s115-115(3)): all days less non-resident days
      after 8 May 2012.
    * Bought earlier, abroad on 8 May 2012 (s115-115(6)): resident days after 8 May 2012.
      The s115-115(4) market value election is not implemented.

    A day no residency period covers is not a resident day in any of the three. It used
    to count as resident in the second case only.
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
        away = residency.days_not_resident(
            account, first_apportionable_day, sale_date, declared=declared)
        counted = total_days - away
    else:
        counted = residency.resident_days(
            account, first_apportionable_day, sale_date, declared=declared)

    counted = max(0, min(counted, total_days))
    fraction = Decimal(counted) / Decimal(total_days)
    return fraction.quantize(_APPORTIONMENT_PRECISION)


def discount_percentage(purchase_date, sale_date, account=None, declared=None):
    """The fraction of a gain the discount removes, as a Decimal (0.5 is half).

    Zero if not held long enough. Otherwise the taxpayer type's rate, apportioned only if
    residency is declared and some day after 8 May 2012 was abroad or undeclared.
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

    # Undeclared days count too, so a gap in the history apportions the discount whether or
    # not some other day was foreign.
    window_start = max(purchase_date, residency.APPORTIONMENT_START_DATE + timedelta(days=1))
    if residency.days_not_resident(account, window_start, sale_date, declared=declared) <= 0:
        return rate

    return rate * apportionment_fraction(
        account, purchase_date, sale_date, declared=declared)


def apply_discount(amount, purchase_date, sale_date, account=None, declared=None):
    """Reduce a gain by its discount percentage. A loss or zero is returned unchanged."""
    if amount is None:
        return amount
    if getattr(amount, 'amount', amount) <= 0:
        return amount
    percentage = discount_percentage(
        purchase_date, sale_date, account=account, declared=declared)
    if percentage == NO_DISCOUNT:
        return amount
    return amount * (Decimal('1') - percentage)
