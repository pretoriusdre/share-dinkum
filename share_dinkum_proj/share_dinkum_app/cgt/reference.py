"""Reference data for classifying instruments.

* `SUFFIX_COUNTRY`, `EXCHANGE_COUNTRY`: market country lookups.
* `SEED_LEGAL_FORMS`: legal forms for widely held ASX codes where they are not obvious
  (e.g. AFI is a company, VAS a unit trust). Deliberately incomplete. Used only as a
  SUGGESTED legal form, which schedules flag as unconfirmed.
"""

from share_dinkum_app.choices import LegalForm

#: yfinance ticker suffix -> ISO country code. No suffix means the US, as in yfinance.
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

#: Widely held ASX codes where the legal form is not obvious. Incomplete by design.
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


def country_for_market(code: str | None = None, suffix: str | None = None) -> str | None:
    """The market's country from its suffix, else its code, or None if unrecognised."""
    if suffix is not None:
        cleaned = str(suffix).strip().lstrip('.').upper()
        if cleaned in SUFFIX_COUNTRY:
            return SUFFIX_COUNTRY[cleaned]
        if cleaned == '':
            return SUFFIX_COUNTRY['']
    if code:
        return EXCHANGE_COUNTRY.get(str(code).strip().upper())
    return None


def seed_legal_form(instrument_name: str | None) -> str | None:
    """A suggested legal form for a well known code, or None."""
    if not instrument_name:
        return None
    return SEED_LEGAL_FORMS.get(str(instrument_name).strip().upper())
