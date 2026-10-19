"""Take personal details out of a document's text, on this machine, before anyone else sees it.

Two layers: the private list of terms in `redact_terms.txt` (names, addresses, phone numbers: what
only the user knows), and masks for the identifiers every statement carries (TFN, HIN/SRN, BSB,
account number, email). Then `review_lines` points at lines that still look personal, by number only.

Nothing here prints a document's text or a term, since a command's output may be read by others.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

DEFAULT_REPLACEMENT = 'REDACTED'

# A holder's own number after its label. A payment's reference number is not one.
HOLDER_LABEL = (r'(?<!payment )\b(?:HIN|SRN|holder (?:identification )?number|securityholder reference number|'
                r'shareholder reference number|holder reference number|security reference number|reference number)\b')

# (pattern, replacement): the identifiers a statement is known to carry. The label is kept. A number
# already all zeros is left alone, so it is not counted twice.
MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), 'redacted@example.com'),
    (re.compile(r'(?i)((?:tax file number|\bTFN)\b[^\n\d]{0,30})\d{3}\s?\d{3}\s?\d{3}'), r'\g<1>000 000 000'),
    # With or without its letter and leading zeros: CommSec prints `HIN 12345678`.
    (re.compile(rf'(?i)({HOLDER_LABEL}\W{{0,5}})[A-Z]?(?!0+(?!\d))\d{{6,12}}(?!\d)'), r'\g<1>X0000000000'),
    # Anywhere, as in a file name such as `PRD_1_X0012345678_NDQ`.
    (re.compile(r'(?<![A-Za-z0-9])[XI](?!0+(?!\d))\d{7,11}(?!\d)'), 'X0000000000'),
    (re.compile(r'\b(?!000-000)\d{3}-\d{3}\b'), '000-000'),
    (re.compile(r'(?i)((?:account\s*(?:number|no\.?)|\bacc(?:\s*no\.?)?\s*:|\bacc\s*no\.?)[^\n\d]{0,10})'
                r'(?!0+(?!\d))\d[\d ]{4,}\d'), r'\g<1>00000000'),
    (re.compile(r'(?i)(customer\s*ref(?:erence)?\.?\s*(?:no|number)\.?\s*:?\s*)(?!0+(?!\d))\d{5,}'), r'\g<1>00000000'),
]

# A state and its postcode: an address, the holder's or the registry's. Reported by how often each
# one appears, not line by line, as a registry's own address is on every page it sends.
POSTCODE = re.compile(r'\b(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4}\b')

# Lines worth a second look after redaction, and why.
REVIEW: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'(?i)^\s*(?:postal |registered |street |residential )?(?:name|address|holder name|account name|'
                r'investor name|phone|mobile|telephone|email)\b'),
     'a name, address or contact label'),
    (re.compile(r'(?<![\d.,])(?:\+61\s?|0)4\d{2}\s?\d{3}\s?\d{3}(?![\d.,])|\(0\d\)\s?\d{4}\s?\d{4}'),
     'a phone number'),
    (re.compile(r'(?<![\d.,])(?!0+(?![\d.,]))\d{8,}(?![\d.,])'), 'a long number, maybe an identifier'),
]


@dataclass(frozen=True)
class Term:
    pattern: re.Pattern[str]
    replacement: str
    line: int


@dataclass
class Redacted:
    text: str
    # How often each term was found, by its line in the list.
    term_counts: Counter[int] = field(default_factory=Counter)
    masked: int = 0


def default_terms_path() -> Path:
    return Path(settings.BASE_DIR) / 'redact_terms.txt'


def load_terms(path: Path) -> list[Term]:
    """The terms in `path`: one per line, `#` for a comment, `term => replacement` to choose the stand-in.

    Longer terms are applied first, so `Jane Smith` is replaced before `Smith`.
    """
    terms = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        term, _, replacement = line.partition('=>')
        term = term.strip()
        if term:
            terms.append(Term(term_pattern(term), replacement.strip() or DEFAULT_REPLACEMENT, number))
    return sorted(terms, key=lambda term: -len(term.pattern.pattern))


def term_pattern(term: str) -> re.Pattern[str]:
    """`term` ignoring case, with any spacing or line breaks between its words, as whole words.

    The space between words may be missing too, as OCR sometimes drops one.
    """
    words = r'\s*'.join(re.escape(word) for word in term.split())
    start = r'(?<!\w)' if term[0].isalnum() else ''
    end = r'(?!\w)' if term[-1].isalnum() else ''
    return re.compile(f'{start}{words}{end}', re.IGNORECASE)


def mask(text: str) -> tuple[str, int]:
    """`text` with the known identifiers replaced, and how many were."""
    count = 0
    for pattern, replacement in MASKS:
        text, made = pattern.subn(replacement, text)
        count += made
    return text, count


def redact(text: str, terms: list[Term]) -> Redacted:
    """`text` with every term replaced, then the known identifiers masked."""
    counts: Counter[int] = Counter()
    for term in terms:
        text, made = term.pattern.subn(term.replacement, text)
        if made:
            counts[term.line] += made
    text, masked = mask(text)
    return Redacted(text, counts, masked)


def redact_path(path: Path, terms: list[Term]) -> Path:
    """`path` (relative) with every term and identifier replaced in each part, since file names carry them too."""
    parts = []
    for part in path.parts:
        for term in terms:
            part = term.pattern.sub(term.replacement, part)
        parts.append(mask(part)[0])
    return Path(*parts)


def postcodes(text: str) -> list[tuple[int, str]]:
    """(line number, state and postcode) for each one in `text`."""
    return [(number, ' '.join(found.split())) for number, line in enumerate(text.splitlines(), start=1)
            for found in POSTCODE.findall(line)]


def review_lines(text: str) -> list[tuple[int, str]]:
    """(line number, reason) for each line that still looks personal."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in REVIEW:
            if pattern.search(line):
                found.append((number, reason))
                break
    return found
