"""Source documents: payment advices, contract notes, annual tax statements and broker exports.

A file is read (`readers`), recognised and parsed by the one parser written for its layout
(`parsers`, or the AI fallback in `ai`), and its events checked against a portfolio (`check`).
Events are rows in the import template's own columns, so loading them later is an import template.
"""

EXPERIMENTAL = (
    'Experimental: reading documents is new, and has been tried on few layouts so far. A parser, OCR or '
    'the AI can misread a figure or miss an event, so check each finding against the document itself '
    'before changing the portfolio.')
