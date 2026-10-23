"""Read every document in a folder into events: by its own parser, or else by the AI if it is on."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from share_dinkum_app.documents import parsers as parsers_package, readers, validate
from share_dinkum_app.documents.extracted import Document, Extracted, Unreadable
from share_dinkum_app.documents.parsers._common import ParseError

if TYPE_CHECKING:
    from share_dinkum_app.documents.ai import Reader


@dataclass
class FileExtraction:
    """What one file gave: its events, or why it gave none."""

    path: Path
    parser: str | None = None
    events: list[Extracted] = field(default_factory=list)
    problem: str | None = None
    text_length: int | None = None
    # Read by OCR, so a figure may be misread.
    scanned: bool = False


def files_in(folder: Path) -> Iterator[Path]:
    """The files of `folder` and every folder inside it, in path order.

    Hidden files and folders, and Office's lock files, are left out.
    """
    for path in sorted(folder.rglob('*')):
        hidden = any(part.startswith('.') for part in path.relative_to(folder).parts)
        if path.is_file() and not hidden and not path.name.startswith('~$'):
            yield path


def extract_folder(folder: Path, ai: 'Reader | None' = None,
                   parsers: list[ModuleType] | None = None) -> list[FileExtraction]:
    """Every file in `folder` and the folders inside it, extracted."""
    if parsers is None:
        parsers = parsers_package.usable(parsers_package.discover())
    return [extract_file(path, parsers, ai) for path in files_in(folder)]


def extract_file(path: Path, parsers: list[ModuleType], ai: 'Reader | None' = None) -> FileExtraction:
    """The events in one file, each validated."""
    doc = readers.read(path)
    if isinstance(doc, Unreadable):
        return FileExtraction(path, problem=doc.reason)
    result = FileExtraction(path, text_length=len(doc.text) if doc.text is not None else None,
                            scanned=doc.scanned)

    found = parsers_package.recognising(doc, parsers)
    if len(found) > 1:
        result.problem = (f'More than one parser recognises this file ({", ".join(p.NAME for p in found)}). '
                          f'Each layout must be recognised by one parser only.')
        return result
    if found:
        result.parser = found[0].NAME
        events = _parse(found[0], doc, result)
    elif ai is not None:
        result.parser = ai.label
        events = _read_with_ai(ai, doc, result)
    else:
        result.problem = 'No parser recognises this layout.'
        return result

    for event in events:
        event.source = path
        event.parser = result.parser or ''
        event.problems = validate.problems(event)
    result.events = events
    if not events and result.problem is None:
        result.problem = f'{result.parser} found no events in it.'
    return result


def _parse(parser: ModuleType, doc: Document, result: FileExtraction) -> list[Extracted]:
    try:
        return list(parser.parse(doc))
    except ParseError as error:
        result.problem = f'{parser.NAME} could not read it: {error}'
    except Exception as error:
        result.problem = f'{parser.NAME} failed on it: {type(error).__name__}: {error}'
    return []


def _read_with_ai(ai: 'Reader', doc: Document, result: FileExtraction) -> list[Extracted]:
    from share_dinkum_app.documents.ai import AIError

    try:
        return ai.extract(doc)
    except AIError as error:
        result.problem = f'{ai.label} could not read it: {error}'
    return []
