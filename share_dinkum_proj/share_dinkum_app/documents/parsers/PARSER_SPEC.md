# Writing a Share Dinkum document parser

Write **one** parser, for the one document layout given under "The document" at the end of this
brief. Deliver the three files below, and two more for each variant, and change nothing else in
the project.

`uv run dev parser_prompt FILE` builds the brief from one example document: this specification,
then the sections after this one (Columns, Document and Extracted, `_common` helpers, The
document), generated from the code, which are part of it. If you are reading `PARSER_SPEC.md` on
its own, it names no document: build the brief first.

The document section is the example's text with personal details already taken out, using the
contributor's `redact_terms.txt` and the identifier masks. Work from that text only, never from the
original document, and read it before sharing the brief: a name missing from the terms list is not
taken out.

## Deliverables

`<name>` is `<issuer>_<kind>` in lower_snake_case. `<issuer>` is the registry, broker or fund
manager whose layout this is. `<kind>` is one of `payment`, `contract_note`, `annual_statement`,
`export`. For example: `computershare_payment`, `commsec_contract_note`,
`vanguard_annual_statement`, `commsec_export`.

All paths are under `share_dinkum_proj/share_dinkum_app/`.

1. `documents/parsers/<name>.py`: the parser.
2. `test_fixtures/documents/<name>.txt` (or `<name>.csv` when `KIND = 'table'`): the document
   below, redacted as described under "The fixture".
3. `test_fixtures/documents/<name>.expected.json`: what `parse()` returns for the fixture.

A layout that changed over the years, such as a broker's notes before and after a redesign, is
still one parser. Each earlier or later form adds a variant, two more files beside the three
above: `<name>--<variant>.txt` (or `.csv`) and `<name>--<variant>.expected.json`, the variant a
short lower-case word such as the year (`commsec_contract_note--2009.txt`). A variant's fixture is
made as "The fixture" describes, and each variant is checked as the main fixture is.

## The module

It has exactly this shape:

```python
"""<Issuer> <document kind>: which documents this reads, in one sentence."""

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = '<name>'  # the file name without .py
KIND = 'text'    # 'text' for a PDF, 'table' for a CSV or spreadsheet export


def recognises(doc: Document) -> bool:
    ...


def parse(doc: Document) -> list[Extracted]:
    ...
```

- It may import only `re`, `datetime`, `decimal`, `typing`, `collections`, `itertools`,
  `functools`, `pandas`, `share_dinkum_app.documents.extracted` and `_common`. It reads no file,
  uses no network or database, and does no logging or printing. `parse` and `recognises` depend
  on `doc` alone.
- Every function is annotated. The file passes `mypy` with the project's settings.
- Use a `_common` helper wherever one fits, rather than writing the same parsing again. Private
  helpers of your own start with an underscore.
- A `text` parser reads `doc.text`, the text `pdfplumber` gives page by page, pages separated by
  `\n\f\n`. A `table` parser reads `doc.table`, a DataFrame whose cells are all text, exactly as
  the file has them.
- A scanned PDF's text is read by OCR (`doc.scanned` is True). Its lines are rebuilt from where
  the text sits on the page, with two spaces between columns, and a space between words is
  sometimes missing. A parser for a scanned layout matches spacing with `\s*` or `\s+`, never a
  fixed number of spaces, and reads a figure only from where its label says it is.

## `recognises(doc)`

- Returns `True` for every document of this layout, and for no other. It never raises.
- It decides from at least two fixed phrases that every document of this layout carries: one
  naming the issuer or registry, and one naming the kind of document, such as its heading. Use
  `_common.has_all`. It never decides from an amount, a date, a name, a code or a reference
  number, since those change from one document to the next.
- A `table` parser decides from the exact header row (`_common.header_is`).

## `parse(doc)`

- Returns one `Extracted` per event, in the order they appear in the document.
- If a value it needs is not there, it raises `_common.ParseError` naming the label it looked
  for. It never returns a partial event and never guesses.

## Which model

- A company dividend → `Dividend`. Fill `dividend_type` (`LOCAL` for an Australian company,
  `FOREIGN` otherwise), `franked_amount_per_share` and `unfranked_amount_per_share` (`0` for a
  part the advice shows as nil), and the withholding if any was taken.
- A trust or ETF distribution → `Distribution`: `distribution_amount_per_share`, `quantity` and
  `total_withholding_tax` if any was taken.
- A contract note → `Buy` or `Sell`. `date` is the trade date, not the settlement date.
  `total_brokerage` is brokerage, GST and every other fee on the trade added together.
- A dividend reinvestment advice → the `Dividend`, then a `Buy` for the shares allotted: dated the
  allotment date, at the issue price, with `total_brokerage` `0`, as the shares are issued for the
  whole amount applied. An ordinary and a special dividend paid together on the same shares are
  one `Dividend`, their rates added. Check the plan's cash account (brought forward plus the
  dividend, less the amount applied, is the balance carried forward) and raise `ParseError` if it
  does not add up.
- An annual tax statement (AMMA, or a trust's annual taxation statement from before AMIT) → one
  `AttributionStatement`, then one `AttributionComponent` for each component the statement gives
  an amount other than nil for, in the order shown. A component is linked to the
  `AttributionStatement` before it, so give none of the link columns.
  - Income and gains come from the attribution column (before AMIT, taxable income). An amount
    printed only as cash (CGT concession, tax-free, tax-deferred, tax-exempt, other capital gains
    distribution, other non-attributable amounts) comes from the cash column.
  - The credits (franking, foreign resident withholding, foreign income tax offset) come from the
    tax return summary (Part A, labels 13Q, 13A and 20O) where the statement has one, as the
    columns of Part B do not always say which figure is which.
  - Where the statement splits one component over several lines, such as NCMI and clean building
    MIT income, or interest subject and not subject to withholding, give the lines' sum.
  - Then a `CostBaseAdjustment`, even when nil: the AMIT cost base net amount, the increase less
    the decrease; before AMIT, minus the tax-deferred amount. Give none if the statement states
    neither.
  - Where the statement prints a subtotal or gross amount, check the lines against it and raise
    `ParseError` if they do not agree, so a line read from the wrong column shows.
- An export of several events → one event per row, using the models above.

## Values

- Give only the columns listed for the model under "Columns", and only those the document states.
  Never fill in a default or a value worked out from other values, beyond the sums and the cost
  base adjustment an annual statement's rules above ask for. Every column marked "required" must
  be given.
- Money and units are `Decimal`, made from the printed text with `_common.money` or
  `_common.units`. Keep every printed decimal place. Never use `float` and never round.
- A rate printed in cents becomes dollars with `_common.cents_to_dollars`, which is exact:
  `143.6788` cents is `Decimal('1.436788')`.
- Dates are `datetime.date`, from `_common.date`. A payment's `date` is its payment date.
- Each money column has a `<column>_currency` column, a code such as `AUD` or `USD`. Give it
  whenever you give the amount.
- `instrument__name` is the exchange code as printed, in upper case: `VAS`, `BHP`.
- `Extracted.reference` is the document's own payment or contract note number, if it prints one.
- `Extracted.stated_total` is the total the document prints for the event before any tax or fee
  is taken off: the gross payment, or units times price on a contract note. Leave it `None` if
  the document prints no such total. The app checks it against the per-unit amount times the
  units, to the cent.
- Leave `source`, `parser` and `problems` alone: the app sets them.

## The fixture

The fixture is the document text under "The document" with these replaced, and nothing else.
Keep line breaks, spacing, labels and headings exactly as they are.

- Names and addresses of holders → `REDACTED NAME`, `REDACTED ADDRESS`.
- HIN or SRN → `X0000000000`. TFN → `000 000 000`. BSB → `000-000`. Account number → `00000000`.
  The brief has already masked the ones it could find; check for any it missed.
- The reference number, and any other number of the holder's own (an order or client number) →
  the same number of digits, different digits.
- Change the units, prices, amounts and dates too, so the fixture records no real trade or
  payment of the contributor's. Change them so every total still adds up.
- Add this as the first line of a `.txt` fixture:
  `FAKE DATA: made-up names, numbers, units, prices and dates, for testing only.`

## `<name>.expected.json`

```json
{
  "events": [
    {
      "model": "Distribution",
      "reference": "000123",
      "stated_total": "1436.79",
      "row": {
        "instrument__name": "VAS",
        "date": "2025-07-16",
        "quantity": "1000",
        "distribution_amount_per_share": "1.436788",
        "distribution_amount_per_share_currency": "AUD"
      }
    }
  ]
}
```

A `Decimal` is written as a string, exactly as `str()` gives it (`Decimal('1000')` is `"1000"`,
`Decimal('0.20')` is `"0.20"`). A date is `YYYY-MM-DD`. `reference` and `stated_total` are `null`
when not given.

## Acceptance

Both of these pass, from `share_dinkum_proj/`:

```bash
python manage.py test share_dinkum_app.tests.DocumentParserContractTests
mypy share_dinkum_app/documents/parsers/<name>.py
```

The contract test checks that:

1. `NAME` is the file name and follows the naming rule, `KIND` is `text` or `table`, both
   functions exist and are annotated, the module has a docstring, and it imports only what is
   allowed.
2. `recognises` accepts its own fixture and variants and rejects every other parser's, and no
   other parser accepts this one's.
3. `parse` gives exactly the events in `<name>.expected.json` for the fixture, and in each
   variant's own expected file for that variant. A `.txt` fixture starts with the fake data line.
4. Every event's model is one listed here, its columns are that model's, the required ones are
   given, values have the right types, and `stated_total` agrees with the per-unit figures.
