import pandas as pd
from datetime import date
from datetime import datetime
import os
from pathlib import Path
import re
from typing import IO, Any, cast

from openpyxl import load_workbook, Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.worksheet.cell_range import CellRange
from openpyxl.styles import Alignment, NamedStyle, Font
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.worksheet.datavalidation import DataValidation

# Annoying data types
from uuid import UUID

from django.db.models.fields.files import FieldFile

import logging
logger = logging.getLogger(__name__)


# The most cells one table, or one sheet read whole, may span. A file can claim a table of
# A1:XFD1048576, and reading it cell by cell would pin the process, so a larger claim is refused.
MAX_TABLE_CELLS = 2_000_000

# Author shown on the notes attached to column headers.
COMMENT_AUTHOR = 'Share Dinkum'
COMMENT_WIDTH = 360   # points
COMMENT_LINE_HEIGHT = 15


# Sheet holding the allowed values behind each dropdown. Excel caps an inline validation list at 255
# characters, and the currency list is far longer, so each list is written to a column of this sheet
# and the validation points at that range. It has no table, so the loader does not read it.
VALUE_ASSISTANCE_SHEET = 'ValueAssistance'

#: A dropdown's allowed values, or a `(list name, values)` pair so several columns share one list.
DropdownValue = str | bool
Dropdown = list[DropdownValue] | tuple[str, list[DropdownValue]]

# Rows of validated cells left below the written data, so rows a person adds by hand still get their
# dropdowns.
DROPDOWN_SPARE_ROWS = 500

# Notes on the header cells of the index sheet.
INDEX_COLUMN_HELP = {
    'sheet_name': 'Number of the sheet that holds the table. Click the link to go to it.',
    'table_name': 'Name of the Excel table, which is also the name of the model it loads into.',
    'description': 'What the table holds.',
    'num_records': 'Rows in the table when the file was written. An empty template table counts its one blank row.',
    'link': 'Click to go to the sheet.',
}


def check_cell_count(description: str, columns: int, rows: int) -> None:
    cells = columns * rows
    if cells > MAX_TABLE_CELLS:
        raise ValueError(
            f'{description} spans {cells:,} cells, and the limit is {MAX_TABLE_CELLS:,}. '
            f'Trim it to its data, and delete any stray formatting far below or beside it.')


# Columns that hold a name or code, which Excel turns into a number if it looks like one.
NAME_COLUMN_SUFFIXES = ('__name', '__code')
NAME_COLUMNS = ('name', 'code')


def name_as_text(value: Any) -> Any:
    """A name or code cell as text. Excel stores 4013, or 700, as a number, but an instrument is named "4013"."""
    if value is None or isinstance(value, bool):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Text cells stripped of surrounding spaces, and a cell of only spaces made blank.

    A stray space after an instrument's name made it a different name, and one before a file path
    made it a path that does not exist, with nothing on the sheet to show it. Name and code columns are
    read as text, as they name a record.
    """
    df = df.copy()
    for position, column in enumerate(df.columns):
        values = df.iloc[:, position].apply(lambda v: (v.strip() or None) if isinstance(v, str) else v)
        if isinstance(column, str) and (column.endswith(NAME_COLUMN_SUFFIXES) or column in NAME_COLUMNS):
            values = values.apply(name_as_text)
        df.isetitem(position, values.tolist())
    # Blanks as None, not NaN, which is truthy.
    return df.astype(object).where(pd.notna(df), None)


def get_all_tables_in_excel(filename: str | Path | IO[bytes]) -> dict[str, pd.DataFrame]:
    """Read every named table in a workbook into DataFrames, keyed by table name.

    A table expands to take in data below its defined range. A sheet with no tables is
    loaded whole.
    """

    def _sheet_to_dataframe(ws: Worksheet) -> pd.DataFrame:
        data_iter = ws.values
        try:
            header = next(data_iter)
        except StopIteration:
            return pd.DataFrame()

        check_cell_count(f"Sheet '{ws.title}'", ws.max_column, ws.max_row)
        rows = list(data_iter)
        df = pd.DataFrame(rows, columns=header)
        df = clean_frame(make_tz_naive(df))
        df = df.astype(object).where(pd.notna(df), None) # Cast NaN to Nones as NaN is truthy
        df = df.dropna(how='all')
        return df

    wb = load_workbook(filename, data_only=True)
    mapping: dict[str, pd.DataFrame] = {}

    for ws in wb.worksheets:
        if ws.title == VALUE_ASSISTANCE_SHEET:
            continue
        tables = list(ws.tables.values())

        if tables:
            for table in tables:
                cell_range = CellRange(table.ref)
                width = cell_range.max_col - cell_range.min_col + 1
                check_cell_count(f"Table '{table.name}' on sheet '{ws.title}' ({cell_range.coord})",
                                 width, cell_range.max_row - cell_range.min_row + 1)
                check_cell_count(f"The area below table '{table.name}' on sheet '{ws.title}'",
                                 width, max(ws.max_row - cell_range.max_row, 0))
                last_data_row = cell_range.max_row

                for row_idx in range(cell_range.max_row + 1, ws.max_row + 1):
                    row_has_data = any(
                        ws.cell(row=row_idx, column=col_idx).value not in (None, '')
                        for col_idx in range(cell_range.min_col, cell_range.max_col + 1)
                    )
                    if row_has_data:
                        last_data_row = row_idx

                if last_data_row > cell_range.max_row:
                    expanded_range = CellRange(
                        min_col=cell_range.min_col,
                        min_row=cell_range.min_row,
                        max_col=cell_range.max_col,
                        max_row=last_data_row,
                    )
                    logger.warning(
                        "Table '%s' on sheet '%s' expanded from %s to %s to include data below the named table.",
                        table.name,
                        ws.title,
                        cell_range.coord,
                        expanded_range.coord,
                    )
                    table.ref = expanded_range.coord
                    cell_range = expanded_range

                data = ws[cell_range.coord]
                content = [[cell.value for cell in row] for row in data]
                if not content:
                    continue

                header = content[0]
                rest = content[1:]
                df = pd.DataFrame(rest, columns=header)
                df = make_tz_naive(df)
                df = df.astype(object).where(pd.notna(df), None)
                df = clean_frame(df).dropna(how='all')

                mapping[table.name] = df
        else:
            df = _sheet_to_dataframe(ws)
            if not df.empty:
                mapping.setdefault(ws.title, df)
                logger.warning(
                    "Sheet '%s' has no named tables; loaded entire sheet as fallback. Ensure that the sheet is named according to the corresponding model (eg Buy, Sell etc)",
                    ws.title,
                )

    return mapping




def make_tz_naive(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.tz_localize(None)
            df[col] = df[col].apply(lambda x: pd.to_datetime(x, errors='coerce').date())  # type: ignore[arg-type, return-value]
    return df



def header_comment(text: str) -> Comment:
    """A note sized to its text, so a long description is not hidden behind a scroll bar."""
    lines = sum(len(line) // 55 + 1 for line in text.splitlines() or [text])
    return Comment(text, COMMENT_AUTHOR, width=COMMENT_WIDTH, height=max(60, COMMENT_LINE_HEIGHT * lines + 20))


class ExcelGen:
    def __init__(
        self,
        title: str | None = None,
        author: str | None = None,
        description: str | None = None,
        template_info: str | None = None,
        url: str | None = None,
    ) -> None:
        """Start an empty workbook. `template_info` defaults to a standard note."""
        self.excel_illegal_characters_re = re.compile(r'[\000-\010]|[\013-\014]|[\016-\037]')
        
        self.title = title
        self.author = author
        self.description = description
        self.template_info = (
            template_info
            or r'All data has been added into named tables contained on separate sheets. The table names and accompanying descriptions is described in "table_info". Primary keys, if defined, are denoted with red header cells. This exporting script is available in the following repository: https://MODEC-DnA@dev.azure.com/MODEC-DnA/dna-mops-nemo/_git/datamanagement '
        )
        self.url = url

        self.table_summary: list[tuple[int, str, str | None, int]] = []

        self.wb = Workbook()
        first_worksheet = self.wb.worksheets[0]
        self.wb.remove(first_worksheet)
        #self._add_cover()

        self.sheet_counter = 0
        self._value_assistance_columns: dict[str, str] = {}   # list name -> range on the value assistance sheet

        self.excel_illegal_characters_re = re.compile(r'[\000-\010]|[\013-\014]|[\016-\037]')

        self.id_col_style =  NamedStyle(name="uuid")
        self.id_col_style.alignment = Alignment(shrinkToFit=True)


    def _value_assistance_range(self, name: str, values: list[DropdownValue]) -> str:
        """Write `values` as their own column of the value assistance sheet, and return its range.

        A list name used again is written once, so several tables can share it.
        """
        if name in self._value_assistance_columns:
            return self._value_assistance_columns[name]

        if VALUE_ASSISTANCE_SHEET in self.wb.sheetnames:
            ws = self.wb[VALUE_ASSISTANCE_SHEET]
        else:
            ws = self.wb.create_sheet(VALUE_ASSISTANCE_SHEET)

        column_index = len(self._value_assistance_columns) + 1
        column_letter = get_column_letter(column_index)

        header = ws.cell(column=column_index, row=1, value=name)
        header.font = Font(bold=True)
        header.comment = header_comment(f'The values the dropdowns for {name} offer. Edit the dropdown, not this list.')
        for offset, value in enumerate(values, start=2):
            ws.cell(column=column_index, row=offset, value=value)
        ws.column_dimensions[column_letter].width = min(max([len(name)] + [len(str(v)) for v in values]) + 2, 60)

        reference = f'{quote_sheetname(VALUE_ASSISTANCE_SHEET)}!${column_letter}$2:${column_letter}${len(values) + 1}'
        self._value_assistance_columns[name] = reference
        return reference

    def _add_dropdowns(
        self,
        ws: Worksheet,
        columns: list[str],
        dropdowns: 'dict[str, Dropdown]',
        start_row: int,
        start_col: int,
        row_count: int,
    ) -> None:
        for column, spec in dropdowns.items():
            if column not in columns:
                logger.warning('Cannot add a dropdown for %r: it is not a column of this table.', column)
                continue

            list_name, values = spec if isinstance(spec, tuple) else (column, spec)
            # A bool stays a bool, so a pick is an Excel TRUE, which loads; the text "TRUE" does not.
            values = [value if isinstance(value, bool) else str(value)
                      for value in values if value is not None and str(value) != '']
            if not values:
                continue

            # showDropDown is left unset on purpose: openpyxl writes it straight through, and Excel
            # reads a set value as "hide the in-cell arrow", the opposite of what it sounds like.
            validation = DataValidation(
                type='list',
                formula1=self._value_assistance_range(list_name, values),
                allow_blank=True,
                showErrorMessage=True,
                errorTitle='Not an allowed value',
                error=f'Pick a value for {column} from the list.',
            )
            ws.add_data_validation(validation)

            letter = get_column_letter(columns.index(column) + start_col)
            validation.add(f'{letter}{start_row + 1}:{letter}{start_row + max(row_count, 1) + DROPDOWN_SPARE_ROWS}')

    def add_table(
        self,
        df: pd.DataFrame,
        table_name: str,
        description: str | None = None,
        pk: str | list[str] | None = None,
        start_row: int = 1,
        start_col: int = 1,
        position_index: int | None = None,
        style_map: dict[str, Any] | None = None,
        width_map: dict[str, float] | None = None,
        format_map: dict[str, str] | None = None,
        exclude_from_summary: bool = False,
        add_hyperlinks: bool = True,
        value_style_map: dict[Any, Any] | None = None,
        tab_color: str | None = None,
        column_descriptions: dict[str, str] | None = None,
        dropdowns: 'dict[str, Dropdown] | None' = None,
    ) -> None:
        """Add `df` as a named Excel table on a new numbered sheet.

        * `table_name`: the Excel table name (max 30 characters).
        * `pk`: column(s) whose headers are highlighted.
        * `style_map`, `width_map`, `format_map`: per-column style, width, number format.
        * `value_style_map`: cell style by cell value.
        * `exclude_from_summary`: leave the table out of the index sheet.
        * `tab_color`: an RGB hex colour for the sheet's tab, such as 'A6A6A6'.
        * `column_descriptions`: column name to text, shown as a note on that column's header cell.
        * `dropdowns`: column name to its allowed values, or to a `(list name, values)` pair so
          several columns can share one list. The cells get a dropdown, and one outside the list
          is refused. Spare rows below the data are covered too.
        """


        if style_map is None:
            style_map = {}

        if width_map is None:
            width_map = {}
        if format_map is None:
            format_map = {}
        if pk is None:
            pk = []
        elif type(pk) is str:
            pk = [pk]

        if value_style_map is None:
            value_style_map = {}

        self.sheet_counter += 1


        if not exclude_from_summary:
            self.table_summary.append((self.sheet_counter, table_name, description, len(df)))

        
        df = df.copy()
        

        df = df.reset_index(drop=True)

        cols = df.columns

        # Strip out all nan, NaT etc and replace with None:
        df.astype(object).where(df.notnull(), None)

        if table_name == 'table_info':
            sheet_name = 'Index'
        else:
            sheet_name = f'{self.sheet_counter:02}'


        ws = self.wb.create_sheet(sheet_name, index=position_index)
        if tab_color:
            ws.sheet_properties.tabColor = tab_color

        for col_index, col in enumerate(cols):
            cell = ws.cell(column=(col_index + start_col), row=start_row)
            cell.value = col

            description_text = (column_descriptions or {}).get(str(col))
            if description_text:
                cell.comment = header_comment(description_text)

            if col in pk:
                cell.style = 'Accent2'

            cell.alignment = Alignment(vertical='top')

            column_width = width_map.get(col, None)

            if column_width:
                ws.column_dimensions[
                    get_column_letter(col_index + start_col)
                ].width = column_width

        ws.row_dimensions[start_row].height = 32

        for row in df.itertuples():
            for col_index, col in enumerate(cols):
                val_to_print = row[col_index + 1]
                cell = ws.cell(
                    column=(col_index + start_col),
                    row=(row[0] + start_row + 1),
                )

                if isinstance(val_to_print, UUID):
                    val_to_print = str(val_to_print)
                    cell.style = self.id_col_style

                elif hasattr(val_to_print, 'amount') and hasattr(val_to_print, 'currency'):
                    # is a money instance (doing this to avoid the import)
                    val_to_print = val_to_print.amount
                
                elif isinstance(val_to_print, FieldFile):
                    val_to_print = str(val_to_print)
                
                elif type(val_to_print) is str:
                    # Illegal unicode characters. 
                    val_to_print = re.sub(self.excel_illegal_characters_re, '', val_to_print)

                    if val_to_print.startswith('=') and not val_to_print.startswith('=HYPERLINK'):
                        # Append apostrophe before leading equals signs to prevent being interpreted as forumla
                        val_to_print = "'" + val_to_print

                    if val_to_print.startswith('http'):
                        cell.hyperlink = val_to_print
                        cell.style = "Hyperlink"
                

                    if val_to_print.startswith('=HYPERLINK'):
                        cell.style = "Hyperlink"

                cell.value = val_to_print

                # removed  cell.alignment = Alignment(vertical='top')


                style_to_apply = style_map.get(col, {})
                if style_to_apply:
                    cell.style = style_to_apply

                if value_style_map:
                    style_to_apply = value_style_map.get(val_to_print, None)
                    if style_to_apply:
                        cell.style = style_to_apply

                number_format = format_map.get(col, None)
                if number_format:
                    cell.number_format = number_format

        table_range = CellRange(
            min_col=start_col,
            min_row=start_row,
            max_col=start_col + len(cols) - 1,
            max_row=start_row + max(len(df), 1), # need at least one row in the table
        )

        tab = Table(displayName=table_name, ref=table_range.coord)

        table_style = TableStyleInfo(
            name="TableStyleMedium9",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )

        tab.tableStyleInfo = table_style
        ws.add_table(tab)
        ws.freeze_panes = f"A{start_row + 1}"

        if dropdowns:
            self._add_dropdowns(ws, [str(col) for col in cols], dropdowns, start_row, start_col, len(df))

    def save(self, output_path: str | Path) -> None:
        """Add the index sheet, autofit columns, and save to `output_path`."""

        self._add_table_summary()

        for ws in self.wb.worksheets:
            self._autofit_columns(ws)

        self.wb.save(output_path)

    # def _add_cover(self):
    #     metadata_list = [
    #         ('title', self.title),
    #         ('author', self.author),
    #         ('description', self.description),
    #         ('generated_at', date.today().isoformat()),
    #     ]

    #     if self.url:
    #         metadata_list.append(('link', self.url))

    #     if self.template_info:
    #         metadata_list.append(('template_info', self.template_info))

    #     meta_df = pd.DataFrame.from_records(metadata_list, columns=['key', 'value'])

    #     self.add_table(
    #         df=meta_df,
    #         table_name='cover',
    #         pk='key',
    #         width_map={'key': 20, 'value': 100},
    #         exclude_from_summary=True,
    #         value_style_map={self.title: 'Headline 1'},
    #     )

    def _add_table_summary(self) -> None:
        table_summary_df = pd.DataFrame.from_records(
            self.table_summary, columns=['sheet_name', 'table_name', 'description', 'num_records']
        )
        table_summary_df['sheet_name'] = table_summary_df['sheet_name'].apply(lambda x : f'{x:02}')
        table_summary_df['link'] = table_summary_df['sheet_name'].apply(lambda x : f'=HYPERLINK("#\'{x}\'!A1", "{x}")')

        if "_table_info" in self.wb:
            ws_to_remove = self.wb["_table_info"]
            self.wb.remove(ws_to_remove)

        self.add_table(
            df=table_summary_df,
            table_name='table_info',
            pk= None, #'table_name',
            width_map={'sheet_name' : 10, 'table_name': 20, 'description': 100, 'num_records': 16},
            position_index=0,
            exclude_from_summary=True,
            column_descriptions=INDEX_COLUMN_HELP,
        )


    def _autofit_columns(self, ws: Worksheet, max_allowable: int = 80) -> None:
        for col in ws.columns:
            max_length = 0
            column = get_column_letter(cast(int, col[0].column))  # Get the column letter

            for cell in col:
                try:  # Necessary to avoid error on empty cells
                    cell_length = len(str(cell.value))
                    if cell_length > max_length:
                        max_length = cell_length
                    if cell_length > max_allowable:
                        cell.alignment = Alignment(wrap_text=True, vertical='top')
                except:
                    pass

            ws.column_dimensions[column].width = (
                min(max_length, max_allowable) + 2 * 1.1
            )

