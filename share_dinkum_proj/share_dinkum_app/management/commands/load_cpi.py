"""Load quarterly CPI index numbers for cost base indexation from 1 July 2027.

Source: ABS 6401.0 table 2, All groups CPI, Australia (series A2325846C), published about
four weeks after each quarter.
"""

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app.models import CPIIndex


class Command(BaseCommand):
    help = 'Load quarterly CPI index numbers from a CSV of date,index_number rows.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            'csv_file',
            help='CSV with a date column and an index number column. A header row is '
                 'detected and skipped.')
        parser.add_argument(
            '--source', default='ABS 6401.0 series A2325846C',
            help='Recorded against each row, so a disputed cost base can be traced back.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be loaded without writing anything.')

    def handle(self, *args: Any, **options: Any) -> None:
        rows = self._read(options['csv_file'])
        if not rows:
            raise CommandError('No CPI rows found in the file.')

        created = updated = 0
        for quarter_start, index_number in rows:
            if options['dry_run']:
                self.stdout.write(f'  {quarter_start.isoformat()}  {index_number}')
                continue
            _entry, was_created = CPIIndex.objects.update_or_create(
                quarter_start_date=quarter_start,
                defaults={'index_number': index_number, 'source': options['source']},
            )
            created += was_created
            updated += not was_created

        first, last = rows[0][0], rows[-1][0]
        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {len(rows)} quarters, {first.isoformat()} to {last.isoformat()}. '
                f'Nothing written.'))
            return

        self.stdout.write(self.style.SUCCESS(
            f'{created} quarters added, {updated} updated, {first.isoformat()} to '
            f'{last.isoformat()}.'))

        # Indexation runs only from the quarter starting on the cutover (s960-275(1B)), so
        # anything earlier is loaded for completeness and will never be read.
        if last < date(2027, 7, 1):
            self.stdout.write(self.style.WARNING(
                'None of these quarters is on or after 1 July 2027, so no cost base can be '
                'indexed from them yet.'))

    def _read(self, path: str) -> list[tuple[date, Decimal]]:
        rows: list[tuple[date, Decimal]] = []
        with open(path, newline='', encoding='utf-8-sig') as handle:
            for line in csv.reader(handle):
                if len(line) < 2:
                    continue
                parsed = self._parse(line[0], line[1])
                if parsed:
                    rows.append(parsed)
        return sorted(rows)

    def _parse(self, raw_date: str | None, raw_value: str | None) -> tuple[date, Decimal] | None:
        """Parse a row to `(quarter_start, index)`, or None if it is not a data row.

        Any date in a quarter maps to its start; the ABS dates a quarter by its last month.
        """
        raw_date = (raw_date or '').strip()
        raw_value = (raw_value or '').strip()
        if not raw_date or not raw_value:
            return None

        parsed = None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%b-%Y', '%b-%Y', '%Y-%m'):
            try:
                parsed = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            return None

        try:
            value = Decimal(raw_value)
        except InvalidOperation:
            return None

        quarter_start = parsed.replace(month=((parsed.month - 1) // 3) * 3 + 1, day=1)
        return quarter_start, value
