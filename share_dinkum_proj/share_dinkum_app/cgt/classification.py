"""Deriving an instrument's CGT schedule category from its legal form and market.
This classifies capital gains for the Australian CGT schedule.

An instrument with no legal form is UNCLASSIFIED rather than guessed.
"""

from share_dinkum_app.choices import CGTAssetCategory, LegalForm
from share_dinkum_app.cgt import reference

AUSTRALIA = 'AU'

#: Forms that are an interest in a company.
SHARE_LIKE = {LegalForm.COMPANY}

#: Forms that are an interest in a trust. Stapled securities go with units, per the ATO.
UNIT_LIKE = {LegalForm.UNIT_TRUST, LegalForm.STAPLED}


def market_country(market):
    """The country a market is in, preferring what the user recorded over a guess."""
    if market is None:
        return None
    if market.country:
        return str(market.country).strip().upper()
    return reference.country_for_market(code=market.code, suffix=market.suffix)


def asset_category(instrument):
    """The CGT schedule category for gains on this instrument.

    A valid override wins; an invalid one gives UNCLASSIFIED. Otherwise it follows from the
    legal form, the market's country, and whether the market is listed.
    """
    if instrument is None:
        return CGTAssetCategory.UNCLASSIFIED

    override = getattr(instrument, 'cgt_asset_category_override', None)
    if override:
        # The field offers the eight categories as choices, but choices are only enforced
        # by forms, and an Excel import writes straight past them. An override that is not
        # one of the eight is not a category, and printing it on a schedule would put an
        # invented box on a tax return. Unclassified says the answer is unknown, which is
        # true, and makes the schedule report it as a draft.
        return (override if override in CGTAssetCategory.values
                else CGTAssetCategory.UNCLASSIFIED)

    legal_form = getattr(instrument, 'legal_form', LegalForm.UNKNOWN) or LegalForm.UNKNOWN
    if legal_form == LegalForm.UNKNOWN:
        return CGTAssetCategory.UNCLASSIFIED

    market = getattr(instrument, 'market', None)
    country = market_country(market)
    is_australian = country == AUSTRALIA
    is_listed = bool(getattr(market, 'is_exchange_listed', True)) if market else False

    if legal_form == LegalForm.REAL_PROPERTY:
        # Real property is categorised by where the land is, not by any listing.
        return CGTAssetCategory.AU_REAL_ESTATE if is_australian else CGTAssetCategory.OVERSEAS_REAL_ESTATE

    if legal_form == LegalForm.COLLECTABLE:
        return CGTAssetCategory.COLLECTABLES

    if legal_form in SHARE_LIKE:
        if is_australian and is_listed:
            return CGTAssetCategory.AU_LISTED_SHARES
        return CGTAssetCategory.OTHER_SHARES

    if legal_form in UNIT_LIKE:
        if is_australian and is_listed:
            return CGTAssetCategory.AU_LISTED_UNITS
        return CGTAssetCategory.OTHER_UNITS

    # RIGHT_OPTION, DEBT and OTHER all fall to the residual box.
    return CGTAssetCategory.OTHER_ASSETS


def is_real_property(instrument):
    """Whether the instrument's legal form is real property.

    Its gains are residential under s102-6, whose quarantining is not implemented, so the
    schedule warns.
    """
    return getattr(instrument, 'legal_form', None) == LegalForm.REAL_PROPERTY


def suggest_legal_form(instrument, market_data=None):
    """A suggested legal form, or None.

    From the seed list, else a yfinance-style `market_data` quoteType of ETF or MUTUALFUND
    (unit trust). EQUITY is ignored, as it also covers stapled securities and trusts.
    """
    seeded = reference.seed_legal_form(getattr(instrument, 'name', None))
    if seeded:
        return seeded

    if not market_data:
        return None

    quote_type = str(market_data.get('quoteType') or '').strip().upper()
    if quote_type in {'ETF', 'MUTUALFUND'}:
        return LegalForm.UNIT_TRUST
    if quote_type == 'EQUITY':
        # Deliberately not returned as a suggestion. Yahoo reports EQUITY for anything that
        # trades like a share, including the stapled securities and property trusts this
        # classification exists to tell apart, so it carries no useful signal here.
        return None
    return None


def suggest_legal_form_from_activity(instrument):
    """Infer the legal form from income history, or None if there is none.

    Dividends only: company. Distributions only: unit trust. Both: stapled security.
    """
    from share_dinkum_app.models import Distribution, Dividend

    if instrument is None:
        return None

    has_dividends = Dividend.objects.filter(instrument=instrument).exists()
    has_distributions = Distribution.objects.filter(instrument=instrument).exists()

    if has_dividends and has_distributions:
        return LegalForm.STAPLED
    if has_dividends:
        return LegalForm.COMPANY
    if has_distributions:
        return LegalForm.UNIT_TRUST
    return None


def suggested_country(market):
    """A suggested country for a market that has none recorded."""
    if market is None or market.country:
        return None
    return reference.country_for_market(code=market.code, suffix=market.suffix)
