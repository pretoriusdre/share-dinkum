"""Read a file into a `Document`: a PDF's text (by OCR for a scan), or a CSV's or spreadsheet's table.

Files are only ever opened for reading.
"""

import csv
import hashlib
from pathlib import Path

import pandas as pd

from share_dinkum_app.documents import ocr
from share_dinkum_app.documents.extracted import Document, Unreadable

TEXT_SUFFIXES = {'.pdf', '.txt'}
TABLE_SUFFIXES = {'.csv', '.xlsx', '.xls'}

# Between the text of one page and the next.
PAGE_BREAK = '\n\f\n'


def read(path: Path) -> Document | Unreadable:
    """The document in `path`, or why it cannot be read."""
    suffix = path.suffix.lower()
    try:
        if suffix == '.pdf':
            text = pdf_text(path)
            if text.strip():
                return Document(path, 'text', text=text)
            return scanned_pdf(path)
        if suffix == '.txt':
            return Document(path, 'text', text=path.read_text(encoding='utf-8'))
        if suffix in TABLE_SUFFIXES:
            return Document(path, 'table', table=table(path))
    except Exception as error:  # A damaged file is reported with the rest, not raised.
        return Unreadable(path, f'The file could not be opened: {error}')
    return Unreadable(path, 'Not a PDF, CSV or spreadsheet.')


def pdf_text(path: Path) -> str:
    """The text of every page, pages separated by `PAGE_BREAK`."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return PAGE_BREAK.join(page.extract_text() or '' for page in pdf.pages)


def scanned_pdf(path: Path) -> Document | Unreadable:
    """A PDF with no text in it, read by OCR if that is installed."""
    if not ocr.available():
        return Unreadable(path, 'The PDF has no text in it, so it is probably a scan. To read scans by OCR on '
                                'this machine, run: uv sync --inexact --extra ocr')
    text = PAGE_BREAK.join(ocr.pdf_pages(path))
    if not text.strip():
        return Unreadable(path, 'The PDF has no text in it, and OCR found none either.')
    return Document(path, 'text', text=text, scanned=True)


def table(path: Path) -> pd.DataFrame:
    """The first sheet of a spreadsheet, or a CSV, with every cell as text and blanks as ''."""
    if path.suffix.lower() == '.csv':
        frame = csv_table(path)
    else:
        frame = pd.read_excel(path, dtype=str, keep_default_na=False)
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def csv_table(path: Path) -> pd.DataFrame:
    """A CSV's cells as text.

    A file of several tables one under another, each with its own columns (an Interactive Brokers statement), has
    rows of different lengths: each row is padded with blanks to the longest, the extra columns named ''. A file
    that is not UTF-8 is read as Windows-1252, as Excel saves a CSV.
    """
    try:
        path.read_text(encoding='utf-8-sig')
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        encoding = 'cp1252'
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=encoding)
    except pd.errors.ParserError:
        with path.open(newline='', encoding=encoding) as file:
            rows = list(csv.reader(file))
        width = max(len(row) for row in rows)
        header, body = rows[0], rows[1:]
        return pd.DataFrame([row + [''] * (width - len(row)) for row in body],
                            columns=header + [''] * (width - len(header)), dtype=str)


def as_text(doc: Document) -> str:
    """The document as text: its own, or its table as CSV."""
    if doc.table is not None:
        return doc.table.to_csv(index=False)
    return doc.text or ''


def sha256(path: Path) -> str:
    """The file's content hash, as hex."""
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()
