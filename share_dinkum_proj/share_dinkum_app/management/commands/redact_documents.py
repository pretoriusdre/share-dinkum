"""Write a redacted text copy of every document in a folder and the folders inside it, on this machine.

Each PDF becomes `<name>.pdf.txt` and each spreadsheet `<name>.xlsx.csv`; a .txt or .csv keeps its
name. The folder layout is kept, with your terms taken out of folder and file names too. A scanned
PDF is read by OCR when the `ocr` extra is installed. Review the copies, then share only them.

The output names no term, no original file name and no document text: only counts, line numbers and
redacted paths, so it can be shown to anyone.
"""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app.documents import readers, redaction
from share_dinkum_app.documents.extract import files_in
from share_dinkum_app.documents.extracted import Document, Unreadable

TERMS_TEMPLATE = """\
# Terms to take out of your documents, one per line, matched ignoring case and spacing.
# Use `term => stand-in` to choose what replaces it; otherwise it becomes REDACTED.
#
# Jane Citizen => REDACTED NAME
# 12 Example Street => REDACTED ADDRESS
# Exampleville => REDACTED SUBURB
# 0412 345 678
"""


class Command(BaseCommand):
    help = 'Write redacted text copies of the documents in a folder, and the folders inside it.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('folder', type=Path, help='The folder of original documents.')
        parser.add_argument('--out', type=Path, required=True,
                            help='The folder to write the redacted copies to. It must not be inside FOLDER.')
        parser.add_argument('--terms', type=Path,
                            help='Your list of terms to redact. Default: redact_terms.txt beside manage.py.')

    def handle(self, *args: Any, **options: Any) -> None:
        folder = Path(options['folder']).resolve()
        out = Path(options['out']).resolve()
        if not folder.is_dir():
            raise CommandError('FOLDER is not a folder.')
        if out == folder or out.is_relative_to(folder):
            raise CommandError('--out must not be inside FOLDER, or the copies would be read as documents.')
        terms = self._terms(options['terms'])

        out.mkdir(parents=True, exist_ok=True)
        written = scanned = 0
        term_counts: dict[int, int] = {}
        to_review: list[str] = []
        # Each state and postcode: how often, in how many files, and where first. Never the value itself.
        postcodes: dict[str, list[Any]] = {}
        for path in files_in(folder):
            relative = redaction.redact_path(path.relative_to(folder), terms)
            doc = readers.read(path)
            if isinstance(doc, Unreadable):
                self.stdout.write(self.style.WARNING(f'  Skipped {relative}: {self._reason(doc)}'))
                continue
            target, text = self._redacted_copy(doc, relative, out, terms, term_counts)
            written += 1
            if doc.scanned:
                scanned += 1
                self.stdout.write(f'  Read by OCR: {relative}')
            to_review += [f'  {target.relative_to(out)} line {number}: {reason}'
                          for number, reason in redaction.review_lines(text)]
            for value in {value for _, value in redaction.postcodes(text)}:
                postcodes.setdefault(value, [0, 0, None])[1] += 1
            for number, value in redaction.postcodes(text):
                seen = postcodes[value]
                seen[0] += 1
                seen[2] = seen[2] or f'{target.relative_to(out)} line {number}'

        self.stdout.write(self.style.SUCCESS(f'Wrote {written} redacted file(s) to {out}.'))
        if scanned:
            self.stdout.write(f'  {scanned} of them were scans read by OCR, which can misread a name, so a term '
                              f'in them may not be found.')
        unused = sorted({term.line for term in terms} - set(term_counts))
        self.stdout.write(f'  Terms found: {sum(term_counts.values())} time(s), by '
                          f'{len(term_counts)} of the {len(terms)} terms.')
        if unused:
            self.stdout.write(f'  Terms never found (lines of your list): {", ".join(map(str, unused))}.')
        if to_review:
            self.stdout.write(self.style.WARNING(f'{len(to_review)} line(s) still look personal. Check them:'))
            for line in to_review:
                self.stdout.write(line)
        if postcodes:
            self.stdout.write(self.style.WARNING(
                f'{len(postcodes)} different state and postcode(s) are left. Each is an address, yours or a '
                f"registry's. Look at each, and add your suburb and postcode to your terms if it is there:"))
            for times, files, first in sorted(postcodes.values(), key=lambda seen: -seen[0]):
                self.stdout.write(f'  {times} time(s) in {files} file(s), first at {first}')
        self.stdout.write('Read every copy before sharing it: a term spelt another way is not found.')

    def _terms(self, given: Path | None) -> list[redaction.Term]:
        path = Path(given) if given else redaction.default_terms_path()
        if not path.is_file():
            if given is None:
                path.write_text(TERMS_TEMPLATE, encoding='utf-8')
                raise CommandError(f'There was no terms list, so one has been started at {path}. Add your name, '
                                   f'address, phone numbers and anything else personal, then run this again.')
            raise CommandError(f'There is no terms file at {path}.')
        terms = redaction.load_terms(path)
        if not terms:
            raise CommandError(f'{path} lists no terms. Add your name, address and phone numbers to it.')
        return terms

    def _redacted_copy(self, doc: Document, relative: Path, out: Path, terms: list[redaction.Term],
                       term_counts: dict[int, int]) -> tuple[Path, str]:
        def redact(text: str) -> str:
            redacted = redaction.redact(text, terms)
            for line, count in redacted.term_counts.items():
                term_counts[line] = term_counts.get(line, 0) + count
            return redacted.text

        if doc.table is not None:
            table = doc.table.map(lambda cell: redact(cell) if isinstance(cell, str) else cell)
            table.columns = [redact(str(column)) for column in table.columns]
            target = out / (relative if relative.suffix.lower() == '.csv' else relative.with_name(f'{relative.name}.csv'))
            target.parent.mkdir(parents=True, exist_ok=True)
            table.to_csv(target, index=False)
            return target, table.to_csv(index=False)

        text = redact(doc.text or '')
        target = out / (relative if relative.suffix.lower() == '.txt' else relative.with_name(f'{relative.name}.txt'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target, text

    def _reason(self, doc: Unreadable) -> str:
        # An error from opening a file can quote its path, which holds the original name.
        return doc.reason if not doc.reason.startswith('The file could not be opened') else 'it could not be opened.'
