"""Whether a parser meets `parsers/PARSER_SPEC.md`. `DocumentParserContractTests` runs this on every one,
so a contributor writes no test code of their own.
"""

import ast
import difflib
import inspect
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

from share_dinkum_app.documents import columns, readers, validate
from share_dinkum_app.documents.extracted import Extracted, Unreadable

FIXTURES = Path(__file__).resolve().parent.parent / 'test_fixtures' / 'documents'
FIXTURE_SUFFIX = {'text': '.txt', 'table': '.csv'}
DOCUMENT_KINDS = ('payment', 'contract_note', 'annual_statement', 'export')
NAME_PATTERN = re.compile(rf'^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:{"|".join(DOCUMENT_KINDS)})$')

ALLOWED_IMPORTS = {
    're', 'datetime', 'decimal', 'typing', 'collections', 'collections.abc', 'itertools', 'functools', 'pandas',
    'share_dinkum_app.documents.extracted', 'share_dinkum_app.documents.parsers._common',
}


def module_name(parser: ModuleType) -> str:
    return parser.__name__.rsplit('.', 1)[-1]


# Between a parser's name and a variant's name: `commsec_contract_note--2009.txt`.
VARIANT = '--'
# The first line of every text fixture, so no one reads one as a real trade.
FAKE_DATA = 'FAKE DATA: made-up names, numbers, units, prices and dates, for testing only.'


def fixture_path(parser: ModuleType, fixtures: Path = FIXTURES) -> Path:
    return fixtures / f'{parser.NAME}{FIXTURE_SUFFIX.get(parser.KIND, ".txt")}'


def expected_path(parser: ModuleType, fixtures: Path = FIXTURES) -> Path:
    return fixtures / f'{parser.NAME}.expected.json'


def variant_paths(parser: ModuleType, fixtures: Path = FIXTURES) -> list[tuple[Path, Path]]:
    """(fixture, expected) for each variant of the layout, such as an earlier year's, beyond the main one."""
    return [(path, path.with_name(f'{path.stem}.expected.json')) for path in fixture_files(fixtures)
            if path.stem.startswith(f'{parser.NAME}{VARIANT}')]


def parser_name(fixture: Path) -> str:
    """The parser a fixture is for: its name less any variant."""
    return fixture.stem.split(VARIANT, 1)[0]


def event_json(event: Extracted) -> dict[str, Any]:
    """An event as `<name>.expected.json` writes it."""
    return {
        'model': event.model,
        'reference': event.reference,
        'stated_total': columns.to_json(event.stated_total),
        'row': {column: columns.to_json(value) for column, value in event.row.items()},
    }


def problems(parser: ModuleType, parsers: list[ModuleType], fixtures: Path = FIXTURES) -> list[str]:
    """Everything about `parser` that does not meet the spec; empty when it does.

    `parsers` is every parser, to check none of the others claims this one's fixture.
    """
    found = shape_problems(parser)
    if found:
        return found

    main = (fixture_path(parser, fixtures), expected_path(parser, fixtures))
    missing = [f'There is no {path.name} in {fixtures}.' for path in main if not path.is_file()]
    if missing:
        return missing
    for fixture, expected in [main, *variant_paths(parser, fixtures)]:
        found += fixture_problems(parser, parsers, fixture, expected, fixtures)
    return found


def fixture_problems(parser: ModuleType, parsers: list[ModuleType], fixture: Path, expected: Path,
                     fixtures: Path) -> list[str]:
    """Whether `parser` recognises `fixture`, alone, and reads it into the events in `expected`."""
    if not expected.is_file():
        return [f'There is no {expected.name} for {fixture.name}.']
    doc = readers.read(fixture)
    if isinstance(doc, Unreadable):
        return [f'{fixture.name} cannot be read: {doc.reason}']

    found = []
    if fixture.suffix == '.txt' and not fixture.read_text(encoding='utf-8').startswith(FAKE_DATA):
        found.append(f'{fixture.name} does not start with the line "{FAKE_DATA}"')
    if not _recognises(parser, doc):
        found.append(f'recognises() does not accept its own fixture, {fixture.name}.')
    found += [f'{other.NAME} also recognises {fixture.name}. Use phrases only this layout has.'
              for other in parsers if other is not parser and other.KIND == doc.kind and _recognises(other, doc)]
    for other_fixture in fixture_files(fixtures):
        if parser_name(other_fixture) == parser.NAME:
            continue
        other_doc = readers.read(other_fixture)
        if not isinstance(other_doc, Unreadable) and other_doc.kind == parser.KIND and _recognises(parser, other_doc):
            found.append(f'recognises() accepts {other_fixture.name}, which is another layout.')

    try:
        events = list(parser.parse(doc))
    except Exception as error:
        return found + [f'parse() raised on {fixture.name}: {type(error).__name__}: {error}']
    got = [event_json(event) for event in events]
    want = [{'reference': None, 'stated_total': None, **event}
            for event in json.loads(expected.read_text(encoding='utf-8')).get('events', [])]
    if got != want:
        diff = difflib.unified_diff(_lines(want), _lines(got), f'{expected.name} (expected)', 'parse() (got)',
                                    lineterm='')
        found.append(f'parse() does not give the expected events for {fixture.name}:\n' + '\n'.join(diff))
    for number, event in enumerate(events, start=1):
        found += [f'{fixture.name} event {number}: {problem}' for problem in validate.problems(event)]
    return found


def shape_problems(parser: ModuleType) -> list[str]:
    """What the module itself lacks: its names, annotations, docstring and imports."""
    name = module_name(parser)
    found = [f'It does not define {attribute}.' for attribute in ('NAME', 'KIND', 'recognises', 'parse')
             if not hasattr(parser, attribute)]
    if found:
        return found
    if parser.NAME != name:
        found.append(f'NAME is {parser.NAME!r}, but it must be the file name, {name!r}.')
    if not NAME_PATTERN.match(name):
        found.append(f'The name {name!r} must be <issuer>_<kind> in lower_snake_case, the kind one of '
                     f'{", ".join(DOCUMENT_KINDS)}.')
    if parser.KIND not in FIXTURE_SUFFIX:
        found.append(f'KIND is {parser.KIND!r}; it must be "text" or "table".')
    for function_name in ('recognises', 'parse'):
        function = getattr(parser, function_name)
        if not callable(function):
            found.append(f'{function_name} is not a function.')
            continue
        signature = inspect.signature(function)
        if signature.return_annotation is inspect.Signature.empty or any(
                p.annotation is inspect.Parameter.empty for p in signature.parameters.values()):
            found.append(f'{function_name}() must be annotated.')
    source = inspect.getsource(parser)
    tree = ast.parse(source)
    if not ast.get_docstring(tree):
        found.append('The module needs a docstring saying which documents it reads.')
    found += [f'It imports {imported}, which a parser may not.' for imported in imports(tree)
              if imported not in ALLOWED_IMPORTS]
    return found


def imports(tree: ast.Module) -> list[str]:
    """The modules a source imports, with `from package import module` read as `package.module`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == 'share_dinkum_app.documents.parsers':
                found += [f'{node.module}.{alias.name}' for alias in node.names]
            else:
                found.append(node.module)
    return found


def fixture_files(fixtures: Path = FIXTURES) -> list[Path]:
    """The document fixtures, leaving out the expected results."""
    if not fixtures.is_dir():
        return []
    return sorted(path for path in fixtures.iterdir() if path.suffix in FIXTURE_SUFFIX.values())


def orphan_fixtures(parsers: list[ModuleType], fixtures: Path = FIXTURES) -> list[str]:
    """Fixtures no parser is named for."""
    names = {getattr(parser, 'NAME', None) for parser in parsers}
    return [f'{path.name} has no parser named {parser_name(path)}.' for path in fixture_files(fixtures)
            if parser_name(path) not in names]


def _recognises(parser: ModuleType, doc: Any) -> bool:
    try:
        return bool(parser.recognises(doc))
    except Exception:
        return False


def _lines(events: list[dict[str, Any]]) -> list[str]:
    return json.dumps(events, indent=1, sort_keys=True).splitlines()
