"""One module per document layout. Adding a layout means adding a file here; nothing lists them.

Each module defines `NAME`, `KIND`, `recognises(doc)` and `parse(doc)`: see `PARSER_SPEC.md`, which
`uv run dev parser_prompt FILE` turns into a complete brief for a new one. A module whose name starts
with an underscore is a helper, not a parser.
"""

import importlib
import logging
import pkgutil
from types import ModuleType

from share_dinkum_app.documents.extracted import Document

logger = logging.getLogger(__name__)

REQUIRED_NAMES = ('NAME', 'KIND', 'recognises', 'parse')


def discover(package: str = __name__) -> list[ModuleType]:
    """Every parser module in `package`, by file name."""
    found = importlib.import_module(package)
    return [importlib.import_module(f'{package}.{info.name}')
            for info in sorted(pkgutil.iter_modules(found.__path__), key=lambda info: info.name)
            if not info.name.startswith('_')]


def usable(modules: list[ModuleType]) -> list[ModuleType]:
    """The modules that define what a parser must, logging any that do not."""
    ready = []
    for module in modules:
        missing = [name for name in REQUIRED_NAMES if not hasattr(module, name)]
        if missing:
            logger.warning('Parser %s is left out: it does not define %s.', module.__name__, ', '.join(missing))
        else:
            ready.append(module)
    return ready


def recognising(doc: Document, parsers: list[ModuleType]) -> list[ModuleType]:
    """The parsers that say `doc` is theirs. A parser that raises is taken as saying no."""
    found = []
    for parser in parsers:
        if parser.KIND != doc.kind:
            continue
        try:
            if parser.recognises(doc):
                found.append(parser)
        except Exception:
            logger.warning('Parser %s raised on %s while recognising it.', parser.NAME, doc.path, exc_info=True)
    return found
