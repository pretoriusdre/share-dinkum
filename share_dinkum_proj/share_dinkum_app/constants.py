from datetime import date

DEFAULT_CURRENCY = 'AUD'
CGT_DISCOUNT_RATE = 0.5 # 50% discount
CGT_DISCOUNT_THRESHOLD_DAYS = 365 # 365 days
# Treasury Laws Amendment (Tax Reform No. 1) Act 2026, Act No. 49 of 2026. For CGT events
# on or after this date the 50% discount is replaced for individuals and trusts by cost base
# indexation. The date is the legal boundary and never changes; whether the app implements
# the post-cutover regime is a separate question.
CGT_CUTOVER_DATE = date(2027, 7, 1)



# --- The 2027 regime -------------------------------------------------------------------

# The rollout gate is `Account.model_2027_regime`, not a constant here. It was one, and
# that meant editing tracked source to change a setting -- which `uv run update` then
# refuses to pull over, so turning modelling on quietly broke updates. The legal gate,
# CGT_CUTOVER_DATE above, stays a constant: that date is the law and is not a setting.

# CPI, per Subdivision 960-M. A flat rate backend exists for projecting forward past the
# last published quarter, but it is a modelling aid and never the production default: a
# guessed inflation rate silently changes a cost base.
CGT_INDEXATION_METHOD = 'CPI_TABLE'
CGT_INDEXATION_FLAT_RATE = 0.025

# s960-275(1B). Indexation runs only from the quarter starting on the cutover, so nothing
# earned before then is indexed even for an asset bought decades earlier.
CGT_INDEXATION_FIRST_QUARTER = date(2027, 7, 1)

# How a cost base adjustment (AMIT or E4) is indexed. Each is indexed on its own, from the
# quarter it is made: the quarter holding its income year end, or the sale's quarter if the
# sale is in that year (s104-107B(4)), and never before the first indexable quarter. A
# decrease is always indexed this way, as s114-15(3) covers a reduction of the total cost
# base. Whether s114-15(2) also reaches an increase to the total cost base is open (see
# share_dinkum_amit_indexation_question.md).
#   True:  interpretation 1, an increase is indexed from its quarter too (preferred).
#   False: interpretation 2, an increase is added at face value, not indexed.
CGT_INDEX_COST_BASE_INCREASES = True

# The four categories of s102-6. Every gain has to be tagged with exactly one, and the
# s102-5 method statement absorbs losses in this order.
CGT_GAIN_DEFERRED_NON_RESIDENTIAL = 'deferred non-residential capital gain'
CGT_GAIN_DEFERRED_RESIDENTIAL = 'deferred residential capital gain'
CGT_GAIN_NON_RESIDENTIAL = 'non-residential capital gain'
CGT_GAIN_RESIDENTIAL = 'residential capital gain'

# s102-5 Step 1 (a) to (d), verbatim in order. It runs against the taxpayer: the deferred
# gains, which are the ones that kept the 50% discount, absorb losses first, which is the
# least valuable thing a loss can be spent on. Step 2 applies prior year losses in the same
# order. This is not a policy choice the application gets to make.
CGT_LOSS_ABSORPTION_ORDER = [
    CGT_GAIN_DEFERRED_NON_RESIDENTIAL,
    CGT_GAIN_DEFERRED_RESIDENTIAL,
    CGT_GAIN_NON_RESIDENTIAL,
    CGT_GAIN_RESIDENTIAL,
]
