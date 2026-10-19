"""Read every document in a folder and say, for each event in it, whether the portfolio agrees.

Payment advices, contract notes, annual tax statements and broker exports are each read by the parser
written for their layout. With `--ai`, a file no parser recognises is read by the AI the DOCUMENT_AI_*
settings name. It writes nothing to the database.
"""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app.documents import (
    EXPERIMENTAL, ai as ai_module, check as check_module, redaction, report as report_module)
from share_dinkum_app.documents.check import DocumentReport, Status, describe_record
from share_dinkum_app.documents.extract import extract_folder
from share_dinkum_app.management.commands._portfolios import select_accounts

# A local model's default context window is small, and it drops what does not fit without saying so.
LONG_TEXT = 12000


class Command(BaseCommand):
    help = ('Check a folder of payment advices, contract notes and tax statements against a portfolio. '
            + EXPERIMENTAL)

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('folder', type=Path, help='The folder of documents. The folders inside it are read too.')
        parser.add_argument('--account', help='Portfolio name. Needed when there is more than one.')
        parser.add_argument('--out', type=Path, help='Write the findings to this Excel file too.')
        parser.add_argument('--unmatched', action='store_true',
                            help='Also list stored records, within the dates the documents cover, that no '
                                 'document matched.')
        parser.add_argument('--ai', action='store_true',
                            help='Read a file no parser recognises with the AI named by DOCUMENT_AI_PROVIDER.')

    def handle(self, *args: Any, **options: Any) -> None:
        folder = Path(options['folder'])
        if not folder.is_dir():
            raise CommandError(f'{folder} is not a folder.')
        accounts = list(select_accounts(options['account']))
        if len(accounts) != 1:
            raise CommandError('There is more than one portfolio. Name one with --account.')
        account = accounts[0]

        self.stdout.write(self.style.WARNING(EXPERIMENTAL))
        reader = self._reader() if options['ai'] else None
        extractions = extract_folder(folder, reader)
        report = check_module.check(account, extractions, unmatched=options['unmatched'])

        self.stdout.write(self.style.MIGRATE_HEADING(f'{account}: {len(report.files)} file(s) in {folder}'))
        self._write_findings(report)
        long_texts = [file.path.name for file in report.files
                      if reader is not None and reader.is_local and file.extraction.parser == reader.label
                      and (file.extraction.text_length or 0) > LONG_TEXT]
        if long_texts:
            self.stdout.write(self.style.NOTICE(
                f'  {len(long_texts)} file(s) read by a local model have long text ({", ".join(long_texts)}). If '
                f'it reads them badly, raise its context window (Ollama: OLLAMA_CONTEXT_LENGTH).'))
        self._write_summary(report)

        if options['out']:
            report_module.write(report, Path(options['out']), title=f'Document check: {account}')
            self.stdout.write(f'Written to {options["out"]}.')

    def _reader(self) -> 'ai_module.Reader':
        try:
            reader = ai_module.from_settings()
        except ai_module.AIConfigError as error:
            raise CommandError(str(error)) from None
        if reader is None:
            raise CommandError('--ai needs DOCUMENT_AI_PROVIDER set in .env: openai_compatible or anthropic.')
        if not reader.is_local:
            if not reader.terms:
                raise CommandError(
                    f'{reader.label} is not on this machine, and there is no list of your terms to redact '
                    f'({redaction.default_terms_path()}), so your name and address would be sent with each '
                    f'document. Add them to that list (redact_documents starts one), or use a local model.')
            self.stdout.write(self.style.WARNING(
                f'The text of each document no parser recognises will be sent to {reader.label}, with the '
                f'{len(reader.terms)} term(s) of your list and the known identifiers taken out. It leaves this '
                f'machine.'))
        return reader

    def _write_findings(self, report: DocumentReport) -> None:
        for file in report.files:
            if file.extraction.problem:
                self.stdout.write(self.style.ERROR(f'  {file.path.name}: {file.extraction.problem}'))
            for result in file.events:
                if result.status is Status.AGREES:
                    continue
                style = self.style.WARNING if result.status is Status.DIFFERS else self.style.NOTICE
                self.stdout.write(style(f'  {file.path.name}: {result.describe()}: {result.status}'))
                for difference in result.differences:
                    self.stdout.write(f'      {difference.column}: document {difference.document}, '
                                      f'stored {difference.stored}')
                for note in result.notes:
                    self.stdout.write(f'      {note}')
        for record in report.without_document:
            self.stdout.write(self.style.NOTICE(f'  No document for {describe_record(record)}'))

    def _write_summary(self, report: DocumentReport) -> None:
        counts = report.counts()
        order = [str(status) for status in Status] + ['Unreadable']
        summary = ', '.join(f'{counts[name]} {name.lower()}' for name in order if counts[name])
        self.stdout.write(f'  Events: {summary or "none"}.')
        if report.without_document:
            self.stdout.write(f'  {len(report.without_document)} stored record(s) with no document.')
        if all(event.status is Status.AGREES for event in report.events()) and not counts['Unreadable']:
            self.stdout.write(self.style.SUCCESS('  Every document agrees with the portfolio.'))
