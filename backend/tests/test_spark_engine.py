"""
Tests for the Apache Spark path.

The distributed engine has to reach the same conclusions as the in-memory one:
the same target column, the same chronological split, the same segments in the
same units. These check that, on the real Spark stages the API calls - not on a
convenience wrapper that production never touches.

Spark needs a JDK and takes tens of seconds to start, so the whole module skips
when Java is unavailable. That keeps `pytest` fast and green on a machine
without a JDK while still covering the path when one is present.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services import analytics_policy as policy


def _spark_available() -> bool:
    try:
        import pyspark  # noqa: F401
        from services.spark_engine import _ensure_java
        _ensure_java()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _spark_available(),
    reason="Spark path needs pyspark and a JDK (JAVA_HOME or a JDK in Program Files)",
)


@pytest.fixture(scope="module")
def retail() -> pd.DataFrame:
    """Same shape as the pandas suite's fixture, so results are comparable."""
    rng = np.random.default_rng(42)
    n = 2000
    dates = pd.date_range("2024-01-01", periods=365)
    df = pd.DataFrame({
        "InvoiceDate": rng.choice(dates, n),
        "CustomerID": rng.integers(1, 120, n),
        "Product": rng.choice(["Widget", "Gadget", "Doohickey"], n),
        "Quantity": rng.integers(1, 20, n),
        "UnitPrice": rng.uniform(2, 50, n).round(2),
    })
    df["Sales"] = df["Quantity"] * df["UnitPrice"] + rng.normal(0, 5, n)
    df = pd.concat([df, df.head(10)])                              # duplicates
    df.loc[df.sample(50, random_state=1).index, "UnitPrice"] = np.nan  # nulls
    return df


@pytest.fixture(scope="module")
def pipeline(retail):
    """Run the ETL and both ML stages exactly as the API router does."""
    from services.spark_engine import spark_etl, spark_forecasting, spark_segmentation

    sdf, cleaned, time_col = spark_etl(retail)
    try:
        yield {
            "cleaned": cleaned,
            "time_col": time_col,
            "forecasting": spark_forecasting(sdf, time_col),
            "segmentation": spark_segmentation(sdf, time_col),
        }
    finally:
        sdf.unpersist()


class TestSparkETL:
    def test_duplicates_removed_and_nulls_filled(self, pipeline, retail):
        cleaned = pipeline["cleaned"]
        assert len(cleaned) == len(retail) - 10
        assert cleaned.isnull().sum().sum() == 0

    def test_date_column_detected(self, pipeline):
        assert pipeline["time_col"] == "InvoiceDate"


class TestSparkForecasting:
    def test_three_model_families_evaluated(self, pipeline):
        fc = pipeline["forecasting"]
        assert not fc["skipped"], fc.get("skip_reason")
        assert set(fc["models"]) == {"Linear Regression", "Random Forest",
                                     "Gradient Boosting"}

    def test_every_model_reports_all_three_metrics(self, pipeline):
        for name, m in pipeline["forecasting"]["models"].items():
            assert {"rmse", "mae", "r2"} <= set(m), f"{name} missing a metric"

    def test_same_target_as_the_pandas_engine(self, pipeline):
        """Both engines share policy.pick_target, so they must agree - if they
        diverge, the Spark numbers are not comparable to the pandas ones."""
        assert pipeline["forecasting"]["target_column"] == "Sales"

    def test_split_ratio_matches_policy(self, pipeline):
        fc = pipeline["forecasting"]
        total = fc["train_rows"] + fc["test_rows"]
        assert fc["train_rows"] == policy.split_index(total)

    def test_models_learn_the_relationship(self, pipeline):
        fc = pipeline["forecasting"]
        assert fc["models"][fc["best_model"]]["r2"] > 0.8


class TestSparkSegmentation:
    def test_rfm_detected_from_customer_date_and_money(self, pipeline):
        seg = pipeline["segmentation"]
        assert not seg["skipped"], seg.get("skip_reason")
        assert "RFM" in seg["method"]

    def test_k_chosen_within_policy_range(self, pipeline):
        seg = pipeline["segmentation"]
        assert seg["k"] in policy.k_candidates(seg["n_entities"])
        assert -1.0 <= seg["silhouette"] <= 1.0

    def test_segment_sizes_sum_to_population(self, pipeline):
        seg = pipeline["segmentation"]
        assert sum(p["size"] for p in seg["segments"]) == seg["n_entities"]

    def test_every_customer_is_segmented(self, pipeline, retail):
        """RFM aggregates one row per customer over that customer's whole
        history. If the frame were row-sampled first, entities would go
        missing and frequency/monetary would come back deflated."""
        assert pipeline["segmentation"]["n_entities"] == retail["CustomerID"].nunique()

    def test_profiles_reported_in_original_units(self, pipeline):
        """Clustering runs on log-scaled values; profiles must not be."""
        seg = pipeline["segmentation"]
        assert any(p["profile"]["monetary"] > 100 for p in seg["segments"]), \
            "monetary profiles look log-scaled, not raw"
