"""Deriving an instrument's capital gains schedule category.

The schedule splits assets eight ways, and an investor cannot reasonably be asked which of
eight labels applies to each holding. They can answer one much simpler question -- is this
a company or a trust -- which is printed on every product disclosure statement and annual
tax statement. Combined with where the market is, that one answer produces the category.

Nothing here guesses. An instrument whose legal form has not been set is reported as
unclassified, so an unknown asset stays visibly unknown instead of defaulting into a
plausible looking box on a tax return.
"""

from share_dinkum_app.choices import CGTAssetCategory, LegalForm
from share_dinkum_app.cgt import reference

AUSTRALIA = 'AU'

#: Forms that are an interest in a company.
SHARE_LIKE = {LegalForm.COMPANY}

#: Forms that are an interest in a trust. A stapled security is a share and a unit bound
#: together; the ATO's own instructions put them with units, so they go there.
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

    An explicit override wins. Otherwise the category follows from the legal form, the
    country of the market, and whether the market is a listed exchange.
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
    """Whether this instrument is a direct interest in land.

    Real property brings the residential capital gain categories of s102-6 and the deemed
    sale rules of Subdivision 112-E into play, none of which this application implements.
    Detecting it is what lets the schedule refuse rather than quietly report a wrong figure.
    """
    return getattr(instrument, 'legal_form', None) == LegalForm.REAL_PROPERTY


def suggest_legal_form(instrument, market_data=None):
    """A suggested legal form for an instrument, or None if nothing is known.

    `market_data` is an optional mapping of the kind yfinance returns. It is consulted only
    after the seed list, and treated as weak evidence: quoteType reports EQUITY for stapled
    securities and listed property trusts, both of which are wrong for this purpose.
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
    """Infer the legal form from what the instrument has actually paid.

    Much stronger evidence than any list of codes, because it comes from the user's own
    records rather than from a guess about what they might hold:

    * a company pays **dividends**, and only a company can frank one;
    * a trust pays **distributions**, and attributes rather than distributes its income;
    * a **stapled security** is a share and a unit bound together, so it pays both, which
      is a signature nothing else produces.

    Returns None where there is no income history to reason from, which is the honest
    answer for a holding that has never paid anything.

    A trust and a stapled security land in the same box on the CGT schedule, so confusing
    the two changes no reported figure. Confusing either with a company does.
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
