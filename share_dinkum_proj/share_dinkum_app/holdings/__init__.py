"""Holdings worked out from the facts: parcels, sale allocations and adjustment spreads.

`strategies` holds the rules, shared with the signals that apply them as records are entered.
`replay` works the whole holding out again in date order without touching the database, and
`compare` sets that against what is stored. See `check_holdings`.
"""
