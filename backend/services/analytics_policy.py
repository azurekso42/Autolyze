"""
Analytics policy - the methodology's decisions, in one place.

The project runs the same methodology on two engines: pandas/scikit-learn for
data that fits in memory, Apache Spark/MLlib above the threshold. The
*execution* differs by design. The *policy* must not: if the two engines pick
different target columns or split at different ratios, the Spark results stop
being comparable to the pandas ones and the evaluation means nothing.

So every threshold and column-selection rule the methodology commits to lives
here, and both engines import it. The helpers below take and return column
*names* - never a dataframe - so there is exactly one implementation for both
engines to share rather than one per dialect.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Sequence

# ── Column-name heuristics ──────────────────────────────────────────────────
# A dataset arrives with no schema contract, so roles are inferred from names.

TARGET_HINT  = re.compile(r"sales|revenue|amount|total|spend|demand|price", re.I)
ID_HINT      = re.compile(r"customer|client|buyer|member|user", re.I)
QTY_HINT     = re.compile(r"quantity|qty", re.I)
PRICE_HINT   = re.compile(r"price|unit", re.I)
# The charted measure excludes 'price': a unit price is not a business volume,
# and summing it over time produces a meaningless line.
MEASURE_HINT = re.compile(r"sales|revenue|demand|amount|total|spend", re.I)

# Ordered strongest-first: 'Sales' beats 'Total' when a dataset has both.
TARGET_PRIORITY = ["sales", "revenue", "demand", "amount", "total", "spend", "price"]

# ── Cleaning (methodology 3.2.1) ────────────────────────────────────────────
MIN_NON_NULL_FRACTION = 0.5   # below this a column is dropped, not imputed

# Validation rules. Matched on the column name alone, like every other rule
# here, so both engines reach the same verdict without seeing the data.
# 'price' is deliberately narrower than PRICE_HINT, which also matches 'unit'
# and would otherwise catch a units-sold column and treat it as money.
CLEAN_PRICE_HINT = re.compile(r"price", re.I)
CLEAN_QTY_HINT   = re.compile(r"quantity|qty", re.I)

# A validation rule assumes the values it rejects are mistakes. Past this
# share of the rows they cannot be: a fifth of a dataset is not a typo, it is
# the data encoding a state the rule does not know about - an item not stocked
# that week, a line type that carries no price. Deleting it would not be
# cleaning, it would be discarding a category. The same reasoning governs
# MIN_NON_NULL_FRACTION above: past a point, absence is information.
MAX_VALIDITY_DROP_FRACTION = 0.10

# ── Date detection ──────────────────────────────────────────────────────────
DATE_PARSE_THRESHOLD = 0.8    # fraction that must parse for a column to be a date
DATE_PROBE_ROWS      = 2000   # probed before committing to a full parse

# A table can carry a timestamp without storing one: engineered feature tables
# routinely split it into integer parts. Without this the pipeline finds no
# time column and the chronological split silently degrades to file order,
# which is not chronological at all.
YEAR_HINT  = re.compile(r"^(year|yr)$", re.I)
MONTH_HINT = re.compile(r"^(month|mon|mth)$", re.I)
DAY_HINT   = re.compile(r"^(day|dom|day_of_month)$", re.I)
# The composed column is ordering metadata, not a new measurement: the parts
# it is built from are already columns, so it must not become a feature.
COMPOSED_DATE_COL = "_composed_date"

# ── Forecasting (methodology 3.3.x) ─────────────────────────────────────────
TRAIN_FRACTION         = 0.8
MIN_TRAIN_ROWS         = 10
MIN_TEST_ROWS          = 5
MAX_ENCODE_CARDINALITY = 100  # above this a categorical is dropped, not encoded
MAX_ENCODE_FRACTION    = 0.5  # ... and likewise if it is this unique per row

# ── Segmentation (methodology 3.4.0) ────────────────────────────────────────
K_MIN, K_MAX             = 2, 7   # exclusive upper bound, i.e. k in 2..6
# K-Means converges to a local optimum, so both engines restart it this many
# times per k and keep the best silhouette. Without restarts the answer depends
# on wherever the single initialization happened to land, which on Spark is a
# function of how the input was partitioned rather than of the data.
KMEANS_RESTARTS          = 5
MAX_ID_CARDINALITY_FRAC  = 0.9    # near-unique numeric columns are identifiers
MIN_RFM_CUSTOMERS        = 10
MIN_CLUSTER_ROWS         = 30
MAX_CLUSTER_FEATURES     = 8

# ── Sampling ────────────────────────────────────────────────────────────────
# Cap on the rows a model is trained from, so an upload stays interactive.
# 500,000 is a latency default rather than a measured optimum, so it is
# overridable: raising it is how the cost of the sampling gets measured
# instead of assumed (5.3).
ML_MAX_ROWS = int(os.getenv("AUTOLYZE_ML_MAX_ROWS", "500000"))
RANDOM_SEED = 42              # every sample, split and model start from this


def pick_target(numeric_columns: Iterable[str]) -> str | None:
    """The column to forecast, by name alone.

    Returns the strongest sales-like match, or None when no name qualifies -
    the caller then falls back to the highest-variance column, which needs the
    data and so cannot be decided here.
    """
    named = [c for c in numeric_columns if TARGET_HINT.search(c)]
    if not named:
        return None
    return min(named, key=lambda c: next(
        i for i, kw in enumerate(TARGET_PRIORITY) if kw in c.lower()))


def pick_customer_column(columns: Iterable[str]) -> str | None:
    """The column identifying the entity to segment (RFM's 'who')."""
    return next((c for c in columns if ID_HINT.search(c)), None)


def pick_monetary(numeric_columns: Sequence[str]):
    """How to derive spend from the available numeric columns.

    Returns ("direct", col) when one column already holds it, ("product", qty,
    price) when it has to be reconstructed as quantity x price, or None when
    the dataset carries no money at all. Restricted to numeric columns in both
    cases: a quantity or price held as text cannot be multiplied.
    """
    named = [c for c in numeric_columns if TARGET_HINT.search(c)]
    if named:
        return "direct", named[0]

    qty   = next((c for c in numeric_columns if QTY_HINT.search(c)), None)
    price = next((c for c in numeric_columns if PRICE_HINT.search(c)), None)
    if qty and price:
        return "product", qty, price
    return None


def pick_date_parts(columns: Iterable[str]):
    """The (year, month, day) columns a timestamp can be rebuilt from.

    Returns the triple, or None when the table does not carry all three. Only
    consulted after the ordinary date detection has failed, so a real
    timestamp column always wins.
    """
    cols = list(columns)
    year  = next((c for c in cols if YEAR_HINT.match(c.strip())), None)
    month = next((c for c in cols if MONTH_HINT.match(c.strip())), None)
    day   = next((c for c in cols if DAY_HINT.match(c.strip())), None)
    if year and month and day:
        return year, month, day
    return None


def never_impute(column: str) -> bool:
    """Whether a column's missing values must be dropped rather than filled.

    An identifier names an entity; there is no median customer. Filling one in
    hands every anonymous row to whichever real customer sits at the median,
    inventing a mega-customer whose recency, frequency and monetary values are
    an artifact of the imputation rather than of anyone's behavior. A row with
    no entity to attribute cannot be segmented, so it is dropped.
    """
    return bool(ID_HINT.search(column))


def validity_rule(column: str) -> str | None:
    """The 3.2.1 validation rule a column's values must satisfy, if any.

    Returns "positive" for money (a transaction priced at or below zero is not
    a sale: it is an adjustment, a write-off or a data-entry error),
    "non_negative" for quantities (a negative quantity is a return or a
    cancelled line, not demand), or None where no rule applies.
    """
    if CLEAN_PRICE_HINT.search(column):
        return "positive"
    if CLEAN_QTY_HINT.search(column):
        return "non_negative"
    return None


def rule_is_enforceable(violations: int, n_rows: int) -> bool:
    """Whether a validation rule's failures are rare enough to be errors.

    Applied per column so the decision is made from the data rather than from
    the dataset's name: no engine, and no dataset, is special-cased.
    """
    if n_rows <= 0:
        return False
    return violations <= MAX_VALIDITY_DROP_FRACTION * n_rows


def is_encodable(distinct: int, n_rows: int) -> bool:
    """Whether a categorical column earns a slot as a model feature.

    Too many levels and it is an identifier wearing a category's clothes: it
    memorizes the training rows and predicts nothing.
    """
    return 1 < distinct <= MAX_ENCODE_CARDINALITY and distinct < MAX_ENCODE_FRACTION * n_rows


def is_identifier(distinct: int, n_rows: int) -> bool:
    """Whether a numeric column is near-unique enough to be an identifier.

    Identifiers dominate Euclidean distance while carrying no similarity
    information, so clustering has to exclude them.
    """
    return distinct >= MAX_ID_CARDINALITY_FRAC * n_rows


def split_index(n_rows: int) -> int:
    """Row offset of the chronological train/test boundary."""
    return int(n_rows * TRAIN_FRACTION)


def can_split(n_rows: int) -> bool:
    """Whether the frame supports a train/test split worth evaluating."""
    split = split_index(n_rows)
    return split >= MIN_TRAIN_ROWS and (n_rows - split) >= MIN_TEST_ROWS


def k_candidates(n_entities: int) -> range:
    """The k values to search, bounded by how many entities there are."""
    return range(K_MIN, min(K_MAX, n_entities))


def kmeans_seeds() -> range:
    """Seeds for the K-Means restarts, fixed so runs stay reproducible."""
    return range(RANDOM_SEED, RANDOM_SEED + KMEANS_RESTARTS)
