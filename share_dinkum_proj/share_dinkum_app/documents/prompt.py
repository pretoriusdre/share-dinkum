"""The brief for writing a new parser: `PARSER_SPEC.md`, then sections generated from the code so they
never drift (each model's columns, the classes a parser uses, the `_common` helpers), then the
document's text, redacted.
"""

import inspect
from pathlib import Path

from share_dinkum_app.documents import columns, extracted, readers, redaction
from share_dinkum_app.documents.extracted import Document
from share_dinkum_app.documents.parsers import _common

SPEC = Path(__file__).resolve().parent / 'parsers' / 'PARSER_SPEC.md'


def build(doc: Document, terms: list[redaction.Term] | None = None) -> tuple[str, redaction.Redacted]:
    """The whole brief for `doc`, and what redacting its text replaced."""
    redacted = redaction.redact(readers.as_text(doc), terms or [])
    fence = '```'
    sections = [
        SPEC.read_text(encoding='utf-8').rstrip(),
        '## Columns\n\n' + columns.markdown(),
        f'## Document and Extracted\n\n{fence}python\n{inspect.getsource(extracted)}{fence}',
        '## `_common` helpers\n\n' + common_helpers(),
        f'## The document\n\nKIND is `{doc.kind}`. File: `{redaction.redact_path(Path(doc.path.name), terms or [])}`.'
        f'\n\n{fence}text\n{redacted.text.rstrip()}\n{fence}',
    ]
    return '\n\n'.join(sections) + '\n', redacted


def common_helpers() -> str:
    """The helpers' signatures and docstrings, and the patterns' values."""
    lines = []
    for name, value in vars(_common).items():
        if name.startswith('_'):
            continue
        if (inspect.isfunction(value) or inspect.isclass(value)) and value.__module__ == _common.__name__:
            heading = f'class {name}' if inspect.isclass(value) else f'def {name}{inspect.signature(value)}'
            lines.append(f'- `{heading}`: {inspect.getdoc(value) or ""}')
        elif name.isupper():
            lines.append(f'- `{name} = {value!r}`')
    return '\n'.join(lines)
