"""The document check as a workbook: one sheet per kind of finding."""

from pathlib import Path

import pandas as pd

from share_dinkum_app import excelinterface
from share_dinkum_app.documents import EXPERIMENTAL, columns
from share_dinkum_app.documents.check import DocumentReport, Status, describe_record


def write(report: DocumentReport, path: Path, title: str) -> None:
    """Save `report` to `path` as an Excel workbook."""
    excel = excelinterface.ExcelGen(title=title)
    excel.add_table(pd.DataFrame({'warning': [EXPERIMENTAL]}), 'Warning', EXPERIMENTAL)
    excel.add_table(documents(report), 'Documents', 'Each file: the parser that read it and what was found.')
    excel.add_table(differences(report), 'Differences', 'Events whose stored figures differ from the document.')
    excel.add_table(not_in_app(report), 'NotInApp', 'Events the portfolio does not have, in import template columns.')
    excel.add_table(other_findings(report), 'OtherFindings', 'Events that are invalid or match more than one record.')
    excel.add_table(without_document(report), 'NoDocument',
                    'Stored records in the period the documents cover that no document matched.')
    excel.save(path)


def documents(report: DocumentReport) -> pd.DataFrame:
    rows = []
    for file in report.files:
        statuses = [str(event.status) for event in file.events]
        rows.append({
            'file': str(file.path),
            'parser': file.extraction.parser or '',
            'scanned': 'yes, read by OCR' if file.extraction.scanned else '',
            'events': len(file.events),
            'outcome': file.extraction.problem and 'Unreadable' or summary(statuses),
            'problem': file.extraction.problem or '',
        })
    return pd.DataFrame(rows, columns=['file', 'parser', 'scanned', 'events', 'outcome', 'problem'])


def summary(statuses: list[str]) -> str:
    if statuses and all(status == Status.AGREES for status in statuses):
        return str(Status.AGREES)
    return ', '.join(f'{statuses.count(status)} {status}' for status in dict.fromkeys(statuses))


def differences(report: DocumentReport) -> pd.DataFrame:
    rows = [{'file': str(result.event.source), 'event': result.describe(), 'column': difference.column,
             'document': columns.to_json(difference.document), 'stored': columns.to_json(difference.stored)}
            for result in report.events() if result.status is Status.DIFFERS
            for difference in result.differences]
    return pd.DataFrame(rows, columns=['file', 'event', 'column', 'document', 'stored'])


def not_in_app(report: DocumentReport) -> pd.DataFrame:
    rows = []
    for result in report.events():
        if result.status is Status.NOT_IN_APP:
            event = result.event
            rows.append({'file': str(event.source), 'model': event.model, 'reference': event.reference or '',
                         'note': ' '.join(result.notes),
                         **{column: columns.to_json(value) for column, value in event.row.items()}})
    fixed = ['file', 'model', 'reference', 'note']
    found = list(dict.fromkeys(column for row in rows for column in row if column not in fixed))
    return pd.DataFrame(rows, columns=fixed + found)


def other_findings(report: DocumentReport) -> pd.DataFrame:
    rows = [{'file': str(result.event.source), 'event': result.describe(), 'status': str(result.status),
             'note': ' '.join(result.notes)}
            for result in report.events() if result.status in (Status.INVALID, Status.AMBIGUOUS)]
    return pd.DataFrame(rows, columns=['file', 'event', 'status', 'note'])


def without_document(report: DocumentReport) -> pd.DataFrame:
    rows = [{'record': describe_record(record), 'id': str(record.pk)} for record in report.without_document]
    return pd.DataFrame(rows, columns=['record', 'id'])
