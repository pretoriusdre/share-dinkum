"""Write the brief for a new document parser: the specification, the columns, and the document's text.

The text is redacted with your list in `redact_terms.txt` and the known identifier masks. Paste the
brief into any AI, or follow it by hand. What it asks for is checked by
`DocumentParserContractTests`; see share_dinkum_app/documents/parsers/PARSER_SPEC.md.
"""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app.documents import prompt, readers, redaction
from share_dinkum_app.documents.extracted import Unreadable


class Command(BaseCommand):
    help = 'Write the brief for a parser of a new document layout, from one example of it.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('file', type=Path, help='One document of the new layout.')
        parser.add_argument('--out', type=Path, help='Write the brief to this file rather than to the screen.')
        parser.add_argument('--terms', type=Path,
                            help='Your list of terms to redact. Default: redact_terms.txt, if there is one.')

    def handle(self, *args: Any, **options: Any) -> None:
        terms_path = Path(options['terms']) if options['terms'] else redaction.default_terms_path()
        if options['terms'] and not terms_path.is_file():
            raise CommandError(f'There is no terms file at {terms_path}.')
        terms = redaction.load_terms(terms_path) if terms_path.is_file() else []

        doc = readers.read(Path(options['file']))
        if isinstance(doc, Unreadable):
            raise CommandError(f'The document cannot be read: {doc.reason}')
        brief, redacted = prompt.build(doc, terms)

        if options['out']:
            out = Path(options['out'])
            out.write_text(brief, encoding='utf-8')
            self.stdout.write(f'Written to {out}.')
        else:
            self.stdout.write(brief)
        self.stderr.write(self.style.WARNING(
            f'{sum(redacted.term_counts.values())} term(s) from your list and {redacted.masked} identifier(s) '
            f'were redacted. Read the document section before sharing the brief.'
            + ('' if terms else f' There is no terms list, so names and addresses are not redacted: see '
                                 f'{redaction.default_terms_path().name}.')))
