"""What a parser is given, and what it gives back."""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd

Kind = Literal['text', 'table']


@dataclass(eq=False)
class Document:
    """A file as read: the text of a PDF (or a .txt file), or the table of a CSV or spreadsheet.

    `kind` is 'text' with `text` set, or 'table' with `table` set. Every cell of a table is text,
    exactly as the file has it, so amounts keep their printed decimal places.

    `scanned` is True when the text was read by OCR from a PDF that had none, so a letter or a
    digit may be misread.
    """

    path: Path
    kind: Kind
    text: str | None = None
    table: pd.DataFrame | None = None
    scanned: bool = False


@dataclass
class Extracted:
    """One event read from a document, as a row in the import template's columns for `model`.

    A parser sets `model`, `row`, `reference` and `stated_total`. The rest is set by the app.

    - `row`: the columns the document states, and no others. Money and units are `Decimal`, dates
      are `datetime.date`, everything else is text.
    - `reference`: the document's own payment or contract note number, if it prints one.
    - `stated_total`: the total the document prints for the event before any tax or fee is taken
      off: the gross payment, or units times price on a contract note. The app checks it against
      the per-unit amount times the units.
    """

    model: str
    row: dict[str, Any]
    reference: str | None = None
    stated_total: Decimal | None = None
    source: Path | None = None
    parser: str = ''
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Unreadable:
    """A file that gave no events, and why."""

    source: Path
    reason: str
