"""Reference data for classifying instruments.

Two lookups live here, and the distinction between them matters.

`EXCHANGE_COUNTRY` is a fact about the world: the ASX is in Australia and will not stop
being so. Hardcoding it costs nothing and is never wrong.

`SEED_LEGAL_FORMS` is different. It is a convenience for the codes an Australian investor
is most likely to hold, and it is deliberately incomplete: it covers the widely held ASX
ETFs and listed investment companies, which is where the company-versus-unit-trust
distinction is least obvious and most often got wrong. AFI and VAS are both AUD-quoted ASX
listings, and one is a company while the other is a unit trust.

A seed value is only ever a starting suggestion. It is applied when an instrument is first
created, recorded as SUGGESTED rather than confirmed, and overwritten the moment the user
says otherwise. It is never consulted again after that. Nothing in the tax calculation
trusts this table -- an instrument the user has not confirmed is reported as unclassified
rather than quietly assumed.
"""

from share_dinkum_app.choices import LegalForm

#: yfinance ticker suffix -> ISO 3166-1 alpha-2 country. An instrument with no suffix is
#: listed in the United States, which is the convention yfinance itself uses.
SUFFIX_COUNTRY = {
    '': 'US',
    'AX': 'AU', 'NZ': 'NZ',
    'L': 'GB', 'IR': 'IE',
    'TO': 'CA', 'V': 'CA',
    'HK': 'HK', 'SI': 'SG', 'T': 'JP', 'KS': 'KR', 'TW': 'TW',
    'NS': 'IN', 'BO': 'IN',
    'DE': 'DE', 'F': 'DE', 'PA': 'FR', 'AS': 'NL', 'BR': 'BE',
    'MI': 'IT', 'MC': 'ES', 'LS': 'PT', 'VI': 'AT', 'SW': 'CH',
    'ST': 'SE', 'OL': 'NO', 'CO': 'DK', 'HE': 'FI', 'WA': 'PL',
    'SA': 'BR', 'MX': 'MX', 'JO': 'ZA', 'TA': 'IL',
}

#: Exchange code -> country, for markets recorded by name rather than by suffix.
EXCHANGE_COUNTRY = {
    'ASX': 'AU', 'CHIA': 'AU', 'NSX': 'AU',
    'NZX': 'NZ',
    'NASDAQ': 'US', 'NYSE': 'US', 'NYSEARCA': 'US', 'AMEX': 'US',
    'BATS': 'US', 'CBOE': 'US', 'OTC': 'US',
    'LSE': 'GB', 'AIM': 'GB',
    'TSX': 'CA', 'TSXV': 'CA',
    'HKEX': 'HK', 'SGX': 'SG', 'TSE': 'JP', 'KRX': 'KR',
    'XETRA': 'DE', 'FRA': 'DE', 'EURONEXT': 'NL', 'SIX': 'CH',
    'NSE': 'IN', 'BSE': 'IN', 'JSE': 'ZA', 'TASE': 'IL',
}

# Legal form constants, mirrored from models.Instrument so this module stays importable
# without pulling in Django. models.py is the single source of the choices themselves.
COMPANY = LegalForm.COMPANY
UNIT_TRUST = LegalForm.UNIT_TRUST
STAPLED = LegalForm.STAPLED

#: Widely held ASX codes where the legal form is not obvious from the ticker.
#: Incomplete by design -- see the module docstring.
SEED_LEGAL_FORMS = {
    # Exchange traded funds. Registered managed investment schemes, so unit trusts, even
    # where the fund holds only foreign shares.
    **{code: UNIT_TRUST for code in [
        'VAS', 'VGS', 'VAE', 'VAP', 'VGE', 'VTS', 'VEU', 'VDHG', 'VDGR', 'VDBA', 'VDCO',
        'VSO', 'VLC', 'VHY', 'VAF', 'VBND', 'VACF', 'VISM', 'VVLU', 'VMIN',
        'A200', 'IOZ', 'IVV', 'IOO', 'IEM', 'IAA', 'IJP', 'IEU', 'IHVV', 'IHOO',
        'STW', 'SFY', 'SLF', 'SPY', 'NDQ', 'HACK', 'ROBO', 'ASIA', 'QLTY', 'ETHI',
        'FAIR', 'GOLD', 'QAU', 'AAA', 'BILL', 'FLOT', 'IAF', 'PLUS', 'SUBD',
        'MVW', 'MVA', 'QUAL', 'HNDQ', 'DHHF', 'BGBL', 'IHWL',
    ]},
    # Listed investment companies. Companies, not trusts, despite behaving like funds.
    **{code: COMPANY for code in [
        'AFI', 'ARG', 'AUI', 'BKI', 'MLT', 'WHF', 'DUI', 'CIN', 'AMH',
        'WAM', 'WAX', 'WAA', 'WLE', 'WGB', 'WMI', 'PIC', 'MIR', 'QVE', 'OPH',
    ]},
    # Stapled securities: a share and a unit traded together, common among A-REITs.
    **{code: STAPLED for code in [
        'SCG', 'SGP', 'GMG', 'MGR', 'DXS', 'CHC', 'HPI', 'SCP', 'CQR', 'BWP',
    ]},
}


def country_for_market(code=None, suffix=None):
    """Best guess at the country a market is in, or None if unrecognised.

    The suffix is checked first: it is the yfinance convention the application already
    relies on to fetch prices, so it is the more dependable of the two.
    """
    if suffix is not None:
        cleaned = str(suffix).strip().lstrip('.').upper()
        if cleaned in SUFFIX_COUNTRY:
            return SUFFIX_COUNTRY[cleaned]
        if cleaned == '':
            return SUFFIX_COUNTRY['']
    if code:
        return EXCHANGE_COUNTRY.get(str(code).strip().upper())
    return None


def seed_legal_form(instrument_name):
    """A suggested legal form for a well known code, or None."""
    if not instrument_name:
        return None
    return SEED_LEGAL_FORMS.get(str(instrument_name).strip().upper())
