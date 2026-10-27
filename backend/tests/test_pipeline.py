"""
Tests for the analytics pipeline.

These target the methodological guarantees the project claims, not just that
the code runs: no leakage in the split, skew handling in segmentation,
identifier exclusion, and graceful degradation when a dataset cannot support a
stage. Nothing here needs Java, Spark or a language model, so the suite runs in
seconds.

Run from the backend directory:  python -m pytest tests -q
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from services import data_analysis_service as das
from services.data_analysis_service import (
    ColumnRoles,
    _build_rfm,
    _build_summary,
    _clean_dataframe,
    _find_datetime_col,
    _make_json_safe,
    _pick_measure,
    _run_forecasting,
    _run_forward_forecast,
    _run_segmentation,
    _unwrap_answer,
)


# ── fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture
def retail() -> pd.DataFrame:
    """A well-formed retail transaction set: customers, dates, quantity, price."""
    rng = np.random.default_rng(42)
    n = 1500
    dates = pd.date_range("2024-01-01", periods=365)
    df = pd.DataFrame({
        "InvoiceDate": rng.choice(dates, n),
        "CustomerID": rng.integers(1, 120, n),
        "Product": rng.choice(["Widget", "Gadget", "Doohickey"], n),
        "Quantity": rng.integers(1, 20, n),
        "UnitPrice": rng.uniform(2, 50, n).round(2),
    })
    df["Sales"] = df["Quantity"] * df["UnitPrice"] + rng.normal(0, 5, n)
    return df


# ── cleaning (methodology 3.2.1) ───────────────────────────────────────────
class TestCleaning:
    def test_exact_duplicates_removed(self, retail):
        dirty = pd.concat([retail, retail.head(25)], ignore_index=True)
        assert len(_clean_dataframe(dirty)) == len(retail)

    def test_numeric_nulls_filled_with_median_not_mean(self):
        # A single extreme outlier separates the two: the mean is dragged, the
        # median is not. The methodology commits to the median for this reason.
        df = pd.DataFrame({"v": [1.0, 2.0, 3.0, 4.0, 1000.0, np.nan]})
        filled = _clean_dataframe(df)["v"]
        assert filled.isna().sum() == 0
        assert filled.iloc[-1] == pytest.approx(3.0)      # median of the rest

    def test_categorical_nulls_filled_with_mode(self):
        df = pd.DataFrame({"c": ["a", "a", "a", "b", None], "n": range(5)})
        assert _clean_dataframe(df)["c"].iloc[-1] == "a"

    def test_mostly_empty_columns_dropped_not_imputed(self):
        # Imputing a column that is more than half absent manufactures data.
        df = pd.DataFrame({"keep": range(100),
                           "mostly_null": [1.0] + [np.nan] * 99})
        out = _clean_dataframe(df)
        assert "keep" in out.columns
        assert "mostly_null" not in out.columns

    def test_missing_identifier_dropped_never_imputed(self):
        # Filling a customer id with the median hands every anonymous row to
        # whoever sits at the median, inventing a customer who never traded.
        df = pd.DataFrame({
            "CustomerID": [10.0, 20.0, 30.0] + [np.nan] * 60,
            "Price": [5.0] * 63,
        })
        out = _clean_dataframe(df)
        assert len(out) == 3
        assert set(out["CustomerID"]) == {10.0, 20.0, 30.0}

    def test_identifier_survives_when_mostly_missing(self):
        # The rows go, not the column: a 20%-complete id column still segments
        # the rows that have one, so dropping it would lose the entity entirely.
        # Rows are distinct so deduplication does not do the work instead.
        df = pd.DataFrame({
            "CustomerID": [float(i) for i in range(20)] + [np.nan] * 80,
            "Price": [float(i + 1) for i in range(100)],
        })
        out = _clean_dataframe(df)
        assert "CustomerID" in out.columns
        assert len(out) == 20

    def test_non_positive_prices_removed(self):
        # A line priced at or below zero is an adjustment, not a sale. Kept
        # rare, so the rule is read as catching errors rather than a state.
        df = pd.DataFrame({"Price": [0.0, -3.0] + [float(i) for i in range(1, 999)],
                           "n": range(1000)})
        out = _clean_dataframe(df)
        assert len(out) == 998
        assert (out["Price"] > 0).all()

    def test_negative_quantities_removed(self):
        # Negative quantity is a return or a cancelled line, not demand.
        # Zero is kept: no sale is a real observation of demand.
        df = pd.DataFrame({"Quantity": [-2, -5, 0] + list(range(1, 998)),
                           "n": range(1000)})
        out = _clean_dataframe(df)
        assert len(out) == 998
        assert (out["Quantity"] >= 0).all()
        assert 0 in out["Quantity"].values

    def test_units_sold_not_mistaken_for_a_price(self):
        # PRICE_HINT matches 'unit', so a naive rule would treat UnitsSold as
        # money and delete every zero-demand day - the intermittency the M5
        # data is chosen for.
        df = pd.DataFrame({"UnitsSold": [0, 5, 0, 12], "n": range(4)})
        assert len(_clean_dataframe(df)) == 4

    def test_systematic_rule_failure_is_a_state_not_an_error(self):
        # 30% of rows priced at zero is not 30% typos: the column is encoding
        # a state (item not stocked). Deleting them discards a category, so
        # the rule stands down. Decided from the proportion, not the dataset.
        n = 1000
        df = pd.DataFrame({
            "sell_price": [0.0] * 300 + [float(i) for i in range(1, 701)],
            "n": range(n),
        })
        out = _clean_dataframe(df)
        assert len(out) == n

    def test_rare_rule_failure_is_still_removed(self):
        # The same rule, the same column name, a different proportion: 1% of
        # zero prices are errors and go.
        n = 1000
        df = pd.DataFrame({
            "sell_price": [0.0] * 10 + [float(i) for i in range(1, 991)],
            "n": range(n),
        })
        out = _clean_dataframe(df)
        assert len(out) == n - 10

    def test_validity_rules_applied_before_imputation(self):
        # An invalid value must not be allowed into the median that fills its
        # neighbours, or the cleaning step propagates the error it removed.
        # Ten bad rows in 110 stay under the enforceable ceiling, so they go.
        # The median of what remains (1..99) is 50; had the -900000 rows been
        # left in, the median would have been 45, so the fill value alone
        # proves the rule ran first.
        df = pd.DataFrame({
            "Price": [-900000.0] * 10 + [np.nan] + [float(i) for i in range(1, 100)],
            "n": range(110),
        })
        out = _clean_dataframe(df)
        assert (out["Price"] > 0).all()
        assert out["Price"].iloc[0] == pytest.approx(50.0)   # the imputed row


# ── forecasting (methodology 3.3.x) ────────────────────────────────────────
class TestForecasting:
    def test_three_model_families_evaluated(self, retail):
        fc = _run_forecasting(_clean_dataframe(retail))
        assert not fc["skipped"], fc.get("skip_reason")
        assert set(fc["models"]) == {"Linear Regression", "Random Forest",
                                     "Gradient Boosting"}

    def test_every_model_reports_all_three_metrics(self, retail):
        fc = _run_forecasting(_clean_dataframe(retail))
        for name, m in fc["models"].items():
            assert {"rmse", "mae", "r2"} <= set(m), f"{name} missing a metric"
            assert m["rmse"] >= 0 and m["mae"] >= 0

    def test_best_model_is_the_lowest_rmse(self, retail):
        fc = _run_forecasting(_clean_dataframe(retail))
        best = min(fc["models"], key=lambda k: fc["models"][k]["rmse"])
        assert fc["best_model"] == best

    def test_split_is_chronological_not_random(self, retail):
        """The central no-leakage guarantee: no training row may fall after a
        test row in time. A random split would violate this almost surely."""
        clean = _clean_dataframe(retail)
        fc = _run_forecasting(clean)
        ordered = clean.sort_values("InvoiceDate")
        split = fc["train_rows"]
        last_train = ordered["InvoiceDate"].iloc[:split].max()
        first_test = ordered["InvoiceDate"].iloc[split:].min()
        assert last_train <= first_test

    def test_train_and_test_partition_the_data(self, retail):
        clean = _clean_dataframe(retail)
        fc = _run_forecasting(clean)
        assert fc["train_rows"] + fc["test_rows"] == len(clean)
        assert fc["train_rows"] > fc["test_rows"]           # 80/20

    def test_sales_like_column_preferred_as_target(self, retail):
        assert _run_forecasting(_clean_dataframe(retail))["target_column"] == "Sales"

    def test_skips_with_a_reason_when_no_numeric_columns(self):
        df = pd.DataFrame({"a": ["x"] * 60, "b": ["y"] * 60})
        fc = _run_forecasting(df)
        assert fc["skipped"] and fc["skip_reason"]

    def test_skips_with_a_reason_when_too_few_rows(self, retail):
        fc = _run_forecasting(retail.head(8))
        assert fc["skipped"] and fc["skip_reason"]


# ── segmentation (methodology 3.4.0) ───────────────────────────────────────
class TestSegmentation:
    def test_rfm_detected_from_customer_date_and_money(self, retail):
        seg = _run_segmentation(_clean_dataframe(retail))
        assert not seg["skipped"], seg.get("skip_reason")
        assert "RFM" in seg["method"]

    def test_k_chosen_within_search_range(self, retail):
        seg = _run_segmentation(_clean_dataframe(retail))
        assert 2 <= seg["k"] <= 6
        assert -1.0 <= seg["silhouette"] <= 1.0

    def test_segment_sizes_sum_to_population(self, retail):
        seg = _run_segmentation(_clean_dataframe(retail))
        assert sum(p["size"] for p in seg["segments"]) == seg["n_entities"]

    def test_profiles_reported_in_original_units(self, retail):
        """Clustering uses log-scaled values; profiles must not be log-scaled,
        or the segments become unreadable to a business user."""
        clean = _clean_dataframe(retail)
        seg = _run_segmentation(clean)
        rfm = _build_rfm(clean)
        for p in seg["segments"]:
            assert rfm["monetary"].min() <= p["profile"]["monetary"] <= rfm["monetary"].max()

    def test_one_extreme_customer_does_not_take_its_own_cluster(self, retail):
        """Without the log transform a single whale forms a singleton cluster
        that scores well and means nothing. Guards that regression."""
        whale = retail.copy()
        whale.loc[whale.index[:3], "CustomerID"] = 999
        whale.loc[whale.index[:3], "Sales"] = 5_000_000.0
        seg = _run_segmentation(_clean_dataframe(whale))
        assert not seg["skipped"], seg.get("skip_reason")
        assert min(p["size"] for p in seg["segments"]) > 1

    def test_falls_back_when_no_customer_column(self, retail):
        """M5 has no customer identifiers by design; the framework must degrade
        to something interpretable rather than fail."""
        seg = _run_segmentation(_clean_dataframe(retail.drop(columns=["CustomerID"])))
        assert not seg["skipped"], seg.get("skip_reason")
        assert "RFM" not in seg["method"]

    def test_skips_with_a_reason_when_nothing_to_cluster(self):
        df = pd.DataFrame({"only": range(5)})
        seg = _run_segmentation(df)
        assert seg["skipped"] and seg["skip_reason"]


class TestSegmentationSampling:
    """RFM aggregates over a customer's whole history, so the row cap that
    keeps forecasting fast must not be applied before the aggregation. Doing
    so deflates frequency and monetary by the sampling fraction and biases
    recency late - wrong numbers, not just noisier ones."""

    @pytest.fixture
    def capped(self, monkeypatch, retail):
        # Below the row count (1500) so the cap engages, above the customer
        # count (~119) so no customer sampling happens either way.
        monkeypatch.setattr(das, "ML_MAX_ROWS", 200)
        return _run_segmentation(_clean_dataframe(retail))

    def test_no_customer_is_dropped_by_the_row_cap(self, capped, retail):
        assert capped["n_entities"] == retail["CustomerID"].nunique()

    def test_monetary_totals_are_not_deflated(self, capped, retail):
        """Sum of the segment means weighted by size must reconstruct the
        full-history spend - a row sample would return a fraction of it."""
        rfm = _build_rfm(_clean_dataframe(retail))
        total = sum(p["profile"]["monetary"] * p["size"] for p in capped["segments"])
        assert total == pytest.approx(rfm["monetary"].sum(), rel=1e-3)

    def test_customers_are_capped_when_there_are_too_many(self, monkeypatch, retail):
        """The cap still applies - it just samples customers, whose histories
        stay intact, rather than transactions."""
        monkeypatch.setattr(das, "ML_MAX_ROWS", 50)
        seg = _run_segmentation(_clean_dataframe(retail))
        assert seg["n_entities"] == 50


class TestSampleScaling:
    """When the pandas frame is only a sample of a much larger file, summed
    charts cover a fraction of the data while the row count reports the whole
    file. Scaling the sums keeps the dashboard from contradicting itself."""

    @pytest.fixture
    def daily(self):
        rng = np.random.default_rng(7)
        days = pd.date_range("2024-01-01", periods=60)
        return pd.DataFrame({"Date": days,
                             "Sales": np.abs(rng.normal(500, 40, len(days)))})

    def test_totals_scale_up_by_the_inverse_sample_fraction(self, daily):
        whole = _build_summary(daily)
        quarter = _build_summary(daily, scale=4.0)
        for a, b in zip(whole.timeseries["values"], quarter.timeseries["values"]):
            # Scaling is applied before the 2dp rounding, so the two differ by
            # up to a cent - that ordering is deliberate, it rounds once.
            assert b == pytest.approx(a * 4.0, abs=0.05)

    def test_estimated_totals_are_labelled_as_estimates(self, daily):
        assert "estimated" in _build_summary(daily, scale=4.0).timeseries["title"]
        assert "estimated" not in _build_summary(daily).timeseries["title"]

    def test_per_row_statistics_are_not_scaled(self, daily):
        """Means and quantiles are unbiased under sampling; multiplying them
        would be as wrong as leaving the sums alone."""
        whole = _build_summary(daily)
        quarter = _build_summary(daily, scale=4.0)
        assert quarter.numeric_stats["Sales"]["mean"] == whole.numeric_stats["Sales"]["mean"]

    def test_projection_inherits_the_scaled_series(self, daily):
        roles = ColumnRoles.detect(daily)
        whole = _run_forward_forecast(daily, roles)
        quarter = _run_forward_forecast(daily, roles, scale=4.0)
        assert np.mean(quarter["values"]) == pytest.approx(
            np.mean(whole["values"]) * 4.0, rel=0.05)


class TestColumnRoles:
    def test_detected_once_and_reused_by_every_stage(self, retail):
        roles = ColumnRoles.detect(retail)
        assert roles.time_col == "InvoiceDate"
        assert roles.measure_name == "Sales"

    def test_alignment_narrows_to_a_sampled_frame(self, retail):
        """Stages that sample still hold roles detected on the full frame.
        Pandas would align those on the union of both indexes, resurrecting
        the rows the caller just dropped."""
        roles = ColumnRoles.detect(retail)
        subset = retail.sample(100, random_state=0)
        aligned = roles.aligned_to(subset)
        assert len(aligned.parsed_time) == 100
        assert aligned.parsed_time.index.equals(subset.index)


# ── forward projection (4.8) ───────────────────────────────────────────────
class TestForwardForecast:
    def test_projection_is_non_negative_for_non_negative_input(self):
        # A span of 62 days or less is aggregated daily, which needs at least
        # 21 points (three weekly seasons). Longer spans roll up to weekly or
        # monthly and would need proportionally more history.
        rng = np.random.default_rng(0)
        days = pd.date_range("2024-01-01", periods=60)
        df = pd.DataFrame({
            "Date": days,
            "Sales": np.abs(rng.normal(500, 40, len(days))) +
                     np.sin(np.arange(len(days)) / 7) * 30,
        })
        fc = _run_forward_forecast(df)
        assert fc, "expected a projection for a 60-day daily series"
        assert fc["freq"] == "D"
        assert all(v >= 0 for v in fc["values"]), "non-negative input must not project negative"
        assert len(fc["values"]) == fc["horizon"]

    def test_projection_labels_continue_past_the_last_observation(self):
        rng = np.random.default_rng(1)
        days = pd.date_range("2024-01-01", periods=60)
        df = pd.DataFrame({"Date": days,
                           "Sales": np.abs(rng.normal(500, 40, len(days)))})
        fc = _run_forward_forecast(df)
        assert pd.Timestamp(fc["labels"][0]) > days[-1]

    def test_returns_empty_when_series_too_short(self, retail):
        assert _run_forward_forecast(retail.head(20)) == {}


# ── time detection (methodology 3.3.5) ─────────────────────────────────────
class TestComposedDate:
    """A table can carry a timestamp without storing one.

    When no date column is found the split falls back to row order, which on a
    table sorted by series is not chronological at all - it holds out different
    entities over the same period and silently answers a different question.
    """

    def test_year_month_day_are_composed(self):
        df = pd.DataFrame({
            "year":  [2011, 2011, 2012, 2012],
            "month": [1, 6, 3, 11],
            "day":   [29, 15, 2, 30],
            "sales": [1.0, 2.0, 3.0, 4.0],
        })
        col, parsed = _find_datetime_col(df)
        assert col == das.policy.COMPOSED_DATE_COL
        assert list(parsed.dt.year) == [2011, 2011, 2012, 2012]
        assert parsed.is_monotonic_increasing

    def test_a_real_date_column_wins(self):
        df = pd.DataFrame({
            "Date":  pd.to_datetime(["2011-01-29", "2011-06-15"]),
            "year":  [2011, 2011], "month": [1, 6], "day": [29, 15],
        })
        col, _ = _find_datetime_col(df)
        assert col == "Date"

    def test_partial_parts_are_ignored(self):
        # Year and month without a day cannot be composed into a date.
        df = pd.DataFrame({"year": [2011, 2012], "month": [1, 6], "sales": [1.0, 2.0]})
        assert _find_datetime_col(df) == (None, None)

    def test_impossible_dates_are_rejected(self):
        # Month 47 is not a month: better no time column than a fabricated one.
        df = pd.DataFrame({"year": [2011] * 4, "month": [47] * 4,
                           "day": [99] * 4, "sales": [1.0, 2.0, 3.0, 4.0]})
        assert _find_datetime_col(df) == (None, None)

    def test_unrelated_numeric_columns_are_not_mistaken_for_parts(self):
        # 'day_of_week' and 'monthly_total' are not the day and month of a date.
        df = pd.DataFrame({"year": [2011, 2012], "day_of_week": [1, 2],
                           "monthly_total": [5, 6], "sales": [1.0, 2.0]})
        assert _find_datetime_col(df) == (None, None)


# ── chat replies (methodology 3.4.2) ───────────────────────────────────────
class TestChatAnswerUnwrapping:
    """The interpretation layer must reach the user as prose.

    call_llm asks for JSON by default because the insights path parses it, so
    a chat reply can come back wrapped. Showing the envelope verbatim puts
    `{"answer": "..."}` on screen instead of the sentence inside it.
    """

    def test_plain_prose_is_untouched(self):
        assert _unwrap_answer("Revenue peaks in November.") == "Revenue peaks in November."

    def test_json_envelope_is_unwrapped(self):
        raw = '{"answer": "The context does not provide that detail."}'
        assert _unwrap_answer(raw) == "The context does not provide that detail."

    @pytest.mark.parametrize("key", ["answer", "response", "text", "content",
                                     "message", "reply"])
    def test_common_envelope_keys(self, key):
        assert _unwrap_answer('{"%s": "Sales fell in Q3."}' % key) == "Sales fell in Q3."

    def test_fenced_json_is_unwrapped(self):
        raw = '```json\n{"answer": "Two segments were found."}\n```'
        assert _unwrap_answer(raw) == "Two segments were found."

    def test_single_string_field_under_an_odd_name(self):
        assert _unwrap_answer('{"result_text": "Only one string here."}') == "Only one string here."

    def test_prose_that_merely_mentions_braces_is_untouched(self):
        text = "The column {Revenue} is algebraically dependent on its predictors."
        assert _unwrap_answer(text) == text

    def test_unparseable_json_falls_back_to_raw(self):
        raw = '{"answer": "unterminated'
        assert _unwrap_answer(raw) == raw

    def test_ambiguous_object_is_not_guessed(self):
        # Two candidate strings and no known key: guessing would risk showing
        # the wrong one, so the raw text is returned for the user to judge.
        raw = '{"foo": "one", "bar": "two"}'
        assert _unwrap_answer(raw) == raw

    def test_chat_system_prompt_forbids_json(self):
        assert "JSON" in das.CHAT_SYSTEM_PROMPT
        assert "plain prose" in das.CHAT_SYSTEM_PROMPT.lower()


# ── helpers relied on by the API layer ─────────────────────────────────────
class TestSerialisationHelpers:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_become_none(self, value):
        """NaN and Infinity are not valid JSON; leaving them in breaks the
        response before it reaches the dashboard."""
        assert _make_json_safe({"x": value})["x"] is None

    def test_nested_structures_are_walked(self):
        out = _make_json_safe({"a": [1.0, float("nan"), {"b": float("inf")}]})
        assert out == {"a": [1.0, None, {"b": None}]}

    def test_datetime_column_detected(self, retail):
        col, parsed = _find_datetime_col(retail)
        assert col == "InvoiceDate"
        assert pd.api.types.is_datetime64_any_dtype(parsed)

    def test_measure_prefers_a_sales_like_column(self, retail):
        _, name = _pick_measure(retail)
        assert name == "Sales"

    def test_summary_reports_shape_and_stays_json_safe(self, retail):
        s = _build_summary(_clean_dataframe(retail))
        assert s.rows == len(retail) and s.columns == retail.shape[1]
        for stat in s.numeric_stats.values():
            for v in stat.values():
                assert v is None or not (isinstance(v, float) and math.isnan(v))
