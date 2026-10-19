"""Compare the events read from documents with what a portfolio holds. Reads only; writes nothing.

An event is matched to the stored record with the same natural key (the keys `figures_dump.Keys`
uses): a payment or trade by instrument and date, a statement or cost base adjustment by instrument
and financial year end, and a statement's line by its component within the statement matched just
before it. Several trades or payments on one day are told apart by their units.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.db import models
from djmoney.money import Money

from share_dinkum_app.documents import columns, readers
from share_dinkum_app.documents.extract import FileExtraction
from share_dinkum_app.documents.extracted import Extracted

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, AttributionStatement

CENT = Decimal('0.01')


class Status(StrEnum):
    AGREES = 'Agrees'
    DIFFERS = 'Differs'
    NOT_IN_APP = 'Not in app'
    AMBIGUOUS = 'Ambiguous'
    INVALID = 'Invalid'


@dataclass
class Difference:
    column: str
    document: Any
    stored: Any


@dataclass
class EventResult:
    event: Extracted
    status: Status
    record: models.Model | None = None
    differences: list[Difference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """The event in a few words: `Distribution VAS 2025-07-16`."""
        return describe_event(self.event)


@dataclass
class FileResult:
    extraction: FileExtraction
    events: list[EventResult] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.extraction.path


@dataclass
class DocumentReport:
    files: list[FileResult]
    # Stored records in the period the documents cover that no document matched, when asked for.
    without_document: list[models.Model] = field(default_factory=list)

    def events(self) -> list[EventResult]:
        return [event for file in self.files for event in file.events]

    def counts(self) -> Counter[str]:
        """Events by status, and files that gave none as 'Unreadable'."""
        counted: Counter[str] = Counter(str(event.status) for event in self.events())
        counted['Unreadable'] = sum(1 for file in self.files if file.extraction.problem)
        return counted


def describe_event(event: Extracted) -> str:
    row = event.row
    parts = [event.model, row.get('instrument__name'), row.get('component'),
             row.get('date') or row.get('financial_year_end_date')]
    return ' '.join(str(part) for part in parts if part)


def describe_record(record: models.Model) -> str:
    instrument = getattr(record, 'instrument', None)
    when = getattr(record, 'date', None) or getattr(record, 'financial_year_end_date', None)
    parts = [type(record).__name__, getattr(instrument, 'name', None), when, getattr(record, 'quantity', None)]
    return ' '.join(str(part) for part in parts if part is not None)


def key_column(model: str) -> str | None:
    """The date column a model's events are matched and ranged on."""
    if model in ('Buy', 'Sell', 'Dividend', 'Distribution'):
        return 'date'
    if model in ('AttributionStatement', 'CostBaseAdjustment'):
        return 'financial_year_end_date'
    return None


def check(account: 'Account', extractions: list[FileExtraction], unmatched: bool = False) -> DocumentReport:
    """Each file's events compared with `account`."""
    return Checker(account).run(extractions, unmatched)


class Checker:
    def __init__(self, account: 'Account') -> None:
        from share_dinkum_app.loading import DataLoader

        self.account = account
        self.loader = DataLoader(account=account)
        self.matched: set[Any] = set()

    def run(self, extractions: list[FileExtraction], unmatched: bool) -> DocumentReport:
        report = DocumentReport([self.check_file(extraction) for extraction in extractions])
        if unmatched:
            report.without_document = self.without_document(report.events())
        return report

    def check_file(self, extraction: FileExtraction) -> FileResult:
        result = FileResult(extraction)
        statement: 'AttributionStatement | None' = None
        statement_seen = False
        source_hash: str | None = None
        for event in extraction.events:
            if event.model == 'AttributionComponent':
                checked = self.check_component(event, statement, statement_seen)
            else:
                checked = self.check_event(event)
            if event.model == 'AttributionStatement':
                statement_seen = True
                statement = checked.record  # type: ignore[assignment]
            if checked.record is not None:
                self.matched.add(checked.record.pk)
                if getattr(checked.record, 'file', None):
                    source_hash = source_hash or readers.sha256(extraction.path)
                    if attached_hash(checked.record) == source_hash:
                        checked.notes.append('This document is already attached to it.')
            result.events.append(checked)
        return result

    def check_event(self, event: Extracted) -> EventResult:
        if event.problems:
            return EventResult(event, Status.INVALID, notes=list(event.problems))
        from share_dinkum_app.models import Instrument

        code = event.row['instrument__name']
        instrument, problem = self.loader.find_related(Instrument, self.account, {'name': code})
        if instrument is None:
            if problem == 'ambiguous':
                return EventResult(event, Status.AMBIGUOUS, notes=[f'More than one instrument is named {code}.'])
            near = self.loader.closest_names(Instrument, 'name', code)
            hint = f' Did you mean {" or ".join(near)}?' if near else ''
            return EventResult(event, Status.NOT_IN_APP, notes=[f'This portfolio has no instrument {code}.{hint}'])

        model = columns.model_class(event.model)
        key = key_column(event.model)
        assert key is not None
        candidates = list(model._default_manager.filter(
            account=self.account, instrument=instrument, is_active=True, **{key: event.row[key]}))
        if len(candidates) > 1 and 'quantity' in event.row:
            same_units = [c for c in candidates if getattr(c, 'quantity') == event.row['quantity']]
            if len(same_units) == 1:
                candidates = same_units
        return self.compared(event, candidates)

    def check_component(self, event: Extracted, statement: 'AttributionStatement | None',
                        statement_seen: bool) -> EventResult:
        if event.problems:
            return EventResult(event, Status.INVALID, notes=list(event.problems))
        if not statement_seen:
            return EventResult(event, Status.INVALID,
                               notes=['A statement line must come after the AttributionStatement it belongs to.'])
        if statement is None:
            return EventResult(event, Status.NOT_IN_APP, notes=['Its statement is not in the app.'])
        candidates = list(statement.components.filter(component=event.row['component'], is_active=True))
        return self.compared(event, candidates)

    def compared(self, event: Extracted, candidates: Sequence[models.Model]) -> EventResult:
        if not candidates:
            return EventResult(event, Status.NOT_IN_APP)
        if len(candidates) > 1:
            listed = '; '.join(describe_record(c) for c in candidates)
            return EventResult(event, Status.AMBIGUOUS, notes=[f'{len(candidates)} stored records match: {listed}.'])
        record = candidates[0]
        differences = compare(event, record)
        return EventResult(event, Status.DIFFERS if differences else Status.AGREES, record, differences)

    def without_document(self, events: list[EventResult]) -> list[models.Model]:
        """Stored records of the models the documents gave, within the dates they cover, matched by none."""
        dates: dict[str, list[date]] = {}
        for result in events:
            key = key_column(result.event.model)
            if key and result.status is not Status.INVALID:
                dates.setdefault(result.event.model, []).append(result.event.row[key])

        found: list[models.Model] = []
        for model_name, seen in dates.items():
            key = key_column(model_name)
            assert key is not None
            stored = columns.model_class(model_name)._default_manager.filter(
                account=self.account, is_active=True, **{f'{key}__range': (min(seen), max(seen))},
            ).exclude(pk__in=self.matched).order_by(key, 'pk')
            found += list(stored)
        return found


def compare(event: Extracted, record: models.Model) -> list[Difference]:
    """Each column of the event whose stored value differs."""
    differences = []
    for column, value in event.row.items():
        if column == 'instrument__name' or value is None:
            continue
        stored = stored_value(record, column)
        if not same(event.model, column, value, stored):
            differences.append(Difference(column, value, stored))
    return differences


def stored_value(record: models.Model, column: str) -> Any:
    value = getattr(record, column, None)
    if isinstance(value, Money):
        return value.amount
    if value is not None and column.endswith('_currency'):
        return str(value)
    return value


def same(model: str, column: str, document: Any, stored: Any) -> bool:
    """Whether the stored value agrees with the document's.

    A per-unit figure agrees when it is the document's to the field's decimal places; a whole amount
    when it is within a cent, as the registry rounds a payment to the cent.
    """
    if stored is None:
        return False
    if columns.kind(model, column) != 'decimal':
        return str(document) == str(stored)
    if column in columns.PER_UNIT_COLUMNS:
        places = getattr(columns.field_for(model, column), 'decimal_places', None) or 6
        tolerance = Decimal(1).scaleb(-places) / 2
    else:
        tolerance = CENT
    return abs(Decimal(document) - Decimal(stored)) <= tolerance


def attached_hash(record: models.Model) -> str | None:
    """The content hash of the document stored with `record`, if its file is there."""
    try:
        path = Path(getattr(record, 'file').path)
    except (ValueError, NotImplementedError):
        return None
    return readers.sha256(path) if path.is_file() else None
