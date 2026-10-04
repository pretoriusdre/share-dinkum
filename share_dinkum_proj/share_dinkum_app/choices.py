"""Every fixed vocabulary (TextChoices) in the application.

Here rather than on the models so `cgt` can use them without importing models at module
level. Values are stored and compared, so they must not change; labels can be reworded.
"""

from django.db import models


class TaxpayerType(models.TextChoices):
    """Who owns a portfolio, which decides what discount is available at all (s115-10)."""

    UNDECLARED = 'UNDECLARED', 'Not declared'
    INDIVIDUAL = 'INDIVIDUAL', 'Individual'
    TRUST = 'TRUST', 'Trust'
    PARTNERSHIP = 'PARTNERSHIP', 'Partnership'
    COMPANY = 'COMPANY', 'Company'
    SMSF = 'SMSF', 'Complying superannuation fund'


class LegalForm(models.TextChoices):
    """What an instrument legally is, which decides where a gain is reported."""

    UNKNOWN = 'UNKNOWN', 'Not yet classified'
    COMPANY = 'COMPANY', 'Company - ordinary or preference shares, including listed investment companies'
    UNIT_TRUST = 'UNIT_TRUST', 'Unit trust - most ETFs, managed funds and property trusts'
    STAPLED = 'STAPLED', 'Stapled security - a share and a unit traded together'
    RIGHT_OPTION = 'RIGHT_OPTION', 'Right, option or entitlement'
    DEBT = 'DEBT', 'Note, bond or other debt interest'
    REAL_PROPERTY = 'REAL_PROPERTY', 'Direct interest in real property'
    COLLECTABLE = 'COLLECTABLE', 'Collectable'
    OTHER = 'OTHER', 'Other CGT asset'


class LegalFormSource(models.TextChoices):
    """Where a legal form came from. Only USER counts as confirmed."""

    DEFAULT = 'DEFAULT', 'Not set'
    SUGGESTED = 'SUGGESTED', 'Suggested from the code or market data'
    USER = 'USER', 'Confirmed by you'


class CGTAssetCategory(models.TextChoices):
    """The eight categories on the ATO CGT schedule, in form order. Labels are ATO wording."""

    AU_LISTED_SHARES = 'AU_LISTED_SHARES', 'Shares in Australian listed companies'
    OTHER_SHARES = 'OTHER_SHARES', 'Other shares'
    AU_LISTED_UNITS = 'AU_LISTED_UNITS', 'Units in Australian listed unit trusts'
    OTHER_UNITS = 'OTHER_UNITS', 'Other units'
    AU_REAL_ESTATE = 'AU_REAL_ESTATE', 'Australian real estate'
    OVERSEAS_REAL_ESTATE = 'OVERSEAS_REAL_ESTATE', 'Overseas real estate'
    COLLECTABLES = 'COLLECTABLES', 'Collectables'
    OTHER_ASSETS = 'OTHER_ASSETS', 'Other assets'

    #: Not on the form: the category could not be worked out. Makes a schedule a draft.
    UNCLASSIFIED = 'UNCLASSIFIED', 'Unclassified'

    @classmethod
    def reportable(cls) -> list['CGTAssetCategory']:
        """The eight real categories, excluding UNCLASSIFIED."""
        return [member for member in cls if member != cls.UNCLASSIFIED]

    @classmethod
    def label_for(cls, value: str) -> str:
        """The label for a stored value, or the value itself if unrecognised."""
        try:
            return cls(value).label
        except ValueError:
            return value

    @classmethod
    def reportable_choices(cls) -> list[tuple[str, str]]:
        """Choices excluding UNCLASSIFIED, for the override field."""
        return [(member.value, member.label) for member in cls.reportable()]


class DividendType(models.TextChoices):
    """Whether a dividend is from an Australian or a foreign company."""

    LOCAL = 'LOCAL', 'Local dividend'
    FOREIGN = 'FOREIGN', 'Foreign dividend'


class SellStrategy(models.TextChoices):
    """How a sale picks the parcels it consumes."""

    FIFO = 'FIFO', 'First-in, First-out'
    LIFO = 'LIFO', 'Last-in, First out'
    MIN_CGT = 'MIN_CGT', 'Minimise net capital gain'
    MANUAL = 'MANUAL', 'Manually create allocations'


class AllocationMethod(models.TextChoices):
    """How a cost base adjustment is spread across parcels."""

    QTY_HELD = 'QTY_HELD', 'Allocate to parcels, weighting by (qty * days_held) in the F.Y.'
    MANUAL = 'MANUAL', 'Manually create allocations'


class ResidencyStatus(models.TextChoices):
    """Australian tax residency over a period."""

    RESIDENT = 'RESIDENT', 'Australian resident'
    FOREIGN = 'FOREIGN', 'Foreign resident'
    TEMPORARY = 'TEMPORARY', 'Temporary resident'


class AttributionComponent(models.TextChoices):
    """Lines on an annual trust tax statement (AMMA)."""

    # Capital gains, split by method and by whether the asset the trust sold was taxable
    # Australian property. The TAP split decides whether a foreign resident member can
    # disregard the gain, so it must be kept rather than netted.
    DISCOUNTED_TAP = 'DISCOUNTED_TAP', 'Discounted capital gains - taxable Australian property'
    DISCOUNTED_NTAP = 'DISCOUNTED_NTAP', 'Discounted capital gains - non-taxable Australian property'
    OTHER_TAP = 'OTHER_TAP', 'Other method capital gains - taxable Australian property'
    OTHER_NTAP = 'OTHER_NTAP', 'Other method capital gains - non-taxable Australian property'
    FOREIGN_CG_DISCOUNTED = 'FOREIGN_CG_DISCOUNTED', 'Taxable foreign capital gains - discounted method'
    FOREIGN_CG_OTHER = 'FOREIGN_CG_OTHER', 'Taxable foreign capital gains - other method'
    AMIT_GROSS_UP = 'AMIT_GROSS_UP', 'AMIT CGT gross up amount'
    NET_CAPITAL_GAIN = 'NET_CAPITAL_GAIN', 'Net capital gain'
    TOTAL_CY_CG = 'TOTAL_CY_CG', 'Total current year capital gains'
    OTHER_CG_DISTRIBUTION = 'OTHER_CG_DISTRIBUTION', 'Other capital gains distribution'

    # Cost base movements. Recorded here for completeness; the adjustment that acts on a
    # parcel is CostBaseAdjustment, linked from the statement.
    COSTBASE_INCREASE = 'COSTBASE_INCREASE', 'AMIT cost base net amount - shortfall (increase cost base)'
    COSTBASE_DECREASE = 'COSTBASE_DECREASE', 'AMIT cost base net amount - excess (reduce cost base)'

    # The pre-AMIT equivalents. A statement from before the fund elected into the AMIT
    # regime -- everything up to roughly 2018 -- states these instead, and without them
    # such a statement cannot be transcribed at all. Which line moves a cost base differs
    # between the two, and getting that backwards is the whole reason to record them
    # separately rather than mapping them onto the AMIT names.
    #: Pre-AMIT: reduces the cost base (CGT event E4).
    TAX_DEFERRED = 'TAX_DEFERRED', 'Tax-deferred amount (reduces cost base)'
    #: Does not adjust the cost base (s104-71(3)).
    CGT_CONCESSION = 'CGT_CONCESSION', 'CGT concession amount (does not adjust cost base)'
    #: Reduce only the reduced cost base. Kept as two lines, as statements print them.
    TAX_FREE = 'TAX_FREE', 'Tax-free amount (reduces reduced cost base only)'
    TAX_EXEMPT = 'TAX_EXEMPT', 'Tax-exempt amount (reduces reduced cost base only)'
    #: Cash in excess of the attribution. Reduces the cost base on AMIT statements that give
    #: this instead of an AMIT cost base net amount.
    NON_ATTRIBUTABLE = 'NON_ATTRIBUTABLE', 'Other non-attributable amount (reduces cost base)'

    # Income, which is not a capital gain but arrives on the same statement.
    FRANKED_DISTRIBUTION = 'FRANKED_DISTRIBUTION', 'Franked distributions from trusts'
    FRANKING_CREDIT = 'FRANKING_CREDIT', 'Franking credits'
    UNFRANKED_DISTRIBUTION = 'UNFRANKED_DISTRIBUTION', 'Unfranked distributions'
    INTEREST = 'INTEREST', 'Interest'
    OTHER_INCOME = 'OTHER_INCOME', 'Other income'
    FOREIGN_SOURCE_INCOME = 'FOREIGN_SOURCE_INCOME', 'Assessable foreign source income'
    FOREIGN_INCOME_TAX_OFFSET = 'FOREIGN_INCOME_TAX_OFFSET', 'Foreign income tax offset'
    NON_ASSESSABLE_NON_EXEMPT = 'NON_ASSESSABLE_NON_EXEMPT', 'Non-assessable non-exempt amount'
    WITHHOLDING_CREDIT = 'WITHHOLDING_CREDIT', 'Credit for foreign resident withholding amounts'
    WITHHOLDING_DEDUCTED = 'WITHHOLDING_DEDUCTED', 'Non-resident tax withheld'


class CGTBasis(models.TextChoices):
    """Whether a figure was worked out from declared residency or from an assumption."""

    LEGACY = 'LEGACY', 'Flat 50% discount, residency not declared'
    DIVISION_115 = 'DIVISION_115', 'Division 115, apportioned for declared residency'


class ValuationPurpose(models.TextChoices):
    """Which deemed disposal a valuation was taken for."""

    CUTOVER_2027 = 'CUTOVER_2027', 'Deemed sale on 1 July 2027 (s112-155)'
    DEPARTURE = 'DEPARTURE', 'Ceasing Australian residency (s104-165)'
    ARRIVAL = 'ARRIVAL', 'Becoming an Australian resident (s855-45)'
    PRE_CGT = 'PRE_CGT', 'Pre-CGT asset, deemed sale (s112-175)'
    OTHER = 'OTHER', 'Other'


class ValuationSource(models.TextChoices):
    """Where a valuation came from, so a disputed cost base can be traced."""

    USER = 'USER', 'Entered by the user'
    PRICE_HISTORY = 'PRICE_HISTORY', 'Closing price held by this application'
    MARKET_DATA = 'MARKET_DATA', 'Retrieved from a market data provider'
    APPORTIONED = 'APPORTIONED', 'Apportioned under the s112-185 method'
