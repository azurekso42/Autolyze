"""
Apache Spark analytics engine (capstone methodology).

Spark owns the distributed ETL and model training:
  pandas dataframe -> Spark DataFrame (ingestion)
  -> Spark cleaning (dedup, high-null column drop, median/mode fill)
  -> Spark feature engineering (calendar features, StringIndexer encoding)
  -> Spark MLlib forecasting (LinearRegression / RandomForestRegressor /
     GBTRegressor, chronological split, RMSE / MAE / R2)
  -> Spark MLlib segmentation (K-Means on RFM aggregates, k chosen by
     the Silhouette Coefficient via ClusteringEvaluator)

Runs Spark in local mode - no cluster required, master set by
AUTOLYZE_SPARK_MASTER (default "local[*]"). The caller
(data_analysis_service) falls back to pandas/scikit-learn when Spark or
Java is unavailable.
"""

from __future__ import annotations

import glob
import os
import re
import sys

import pandas as pd

from services import analytics_policy as policy
from services.analytics_policy import ML_MAX_ROWS
from utils.logging_utils import get_logger

logger = get_logger(__name__)

# Executor count is not a performance knob alone: MLlib draws K-Means starting
# centroids and Random Forest bootstrap samples within each data partition, and
# the partitioning follows the master setting, so a fixed seed does not make
# those stages reproducible across different thread counts. "local[*]" resolves
# to however many threads the host happens to have, which makes a run
# non-portable by construction. Pin this to a concrete count (e.g. "local[16]")
# to reproduce a published result on other hardware.
# `or` rather than a getenv default: an env var set to the empty string would
# otherwise be passed through and fail at session start.
SPARK_MASTER = os.getenv("AUTOLYZE_SPARK_MASTER", "").strip() or "local[*]"

_spark = None


def _ensure_java() -> None:
    home = os.environ.get("JAVA_HOME", "")
    if home and os.path.exists(os.path.join(home, "bin", "java.exe")):
        return
    for pattern in (
        r"C:\Program Files\Microsoft\jdk-*",
        r"C:\Program Files\Eclipse Adoptium\jdk-*",
        r"C:\Program Files\Java\jdk-*",
    ):
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits:
            os.environ["JAVA_HOME"] = hits[0]
            logger.info("Spark: using Java at %s", hits[0])
            return
    raise RuntimeError("No Java runtime found (JAVA_HOME unset and no JDK in Program Files).")


def _get_spark():
    """Lazy SparkSession singleton in local mode."""
    global _spark
    if _spark is not None:
        return _spark

    _ensure_java()
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)

    import logging
    logging.getLogger("py4j").setLevel(logging.WARNING)

    from pyspark.sql import SparkSession

    _spark = (
        SparkSession.builder
        .master(SPARK_MASTER)
        .appName("Autolyze")
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.driver.memory", "4g")
        # Spark 4 turns ANSI mode on by default; keep legacy null-on-error
        # semantics (try_to_timestamp/casts) the pipeline was written for
        .config("spark.sql.ansi.enabled", "false")
        .config("spark.ui.enabled", "false")
        # Arrow makes createDataFrame/toPandas columnar (10-100x faster
        # than the default row-by-row pickle serialization)
        .config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .config("spark.sql.execution.arrow.pyspark.fallback.enabled", "true")
        # Both ML stages are submitted concurrently; FAIR scheduling lets
        # them share executor cores instead of queuing FIFO
        .config("spark.scheduler.mode", "FAIR")
        .getOrCreate()
    )
    _spark.sparkContext.setLogLevel("ERROR")

    # Pre-load MLlib modules: forecasting and segmentation run in parallel
    # threads, and concurrent first-imports of pyspark.ml race and fail
    # with a circular-import error.
    import pyspark.ml.clustering    # noqa: F401
    import pyspark.ml.evaluation    # noqa: F401
    import pyspark.ml.feature       # noqa: F401
    import pyspark.ml.regression    # noqa: F401
    from pyspark.sql import Window  # noqa: F401

    logger.info("SparkSession started (master=%s, Spark %s).", SPARK_MASTER, _spark.version)
    return _spark


SUMMARY_SAMPLE_ROWS = 250_000


def spark_etl_file(path: str):
    """Spark-direct ingestion for large CSVs that must not pass through
    pandas first. Reads, sanitizes, cleans and caches the file in Spark;
    only a bounded random sample comes back as pandas for the summary/charts.

    Returns (sdf ready for ML, sample pandas df, time_col, total_rows,
    sample_fraction). The caller needs the realized fraction to scale the
    sample's summed charts back to whole-dataset units.
    """
    from pyspark.sql import functions as F

    spark = _get_spark()

    sdf = spark.read.csv(path, header=True, inferSchema=True)
    sdf = sdf.toDF(*_dedupe([re.sub(r"\W", "_", str(c)).strip("_") or "col" for c in sdf.columns]))

    # Time column: Spark-typed date/timestamp first, else probe string
    # columns on a small pandas sample and convert the winner in Spark.
    time_col = next((c for c, t in sdf.dtypes if t in ("timestamp", "date", "timestamp_ntz")), None)
    if time_col is None:
        probe = sdf.limit(5000).toPandas()
        for col in probe.select_dtypes(include="object").columns:
            s = probe[col].dropna()
            if s.empty:
                continue
            if pd.to_datetime(s, errors="coerce").notna().mean() > 0.8:
                time_col = col
                break
    if time_col is not None and dict(sdf.dtypes).get(time_col) == "string":
        sdf = sdf.withColumn(time_col, F.expr(f"try_to_timestamp(`{time_col}`)"))
    elif time_col is not None and dict(sdf.dtypes).get(time_col) == "date":
        sdf = sdf.withColumn(time_col, F.col(time_col).cast("timestamp"))

    # No timestamp column, but the parts may still be there. Without this the
    # split falls back to file order, which on a table sorted by series is not
    # chronological and quietly evaluates a different question.
    if time_col is None:
        sdf, time_col = _compose_date(sdf, time_col)

    sdf = _spark_clean(sdf).cache()
    total = sdf.count()
    logger.info("Spark direct ingestion: %d rows after cleaning.", total)

    frac = min(1.0, SUMMARY_SAMPLE_ROWS / max(total, 1))
    sample_pdf = (sdf.sample(fraction=frac, seed=42) if frac < 1.0 else sdf).toPandas()
    # Spark's sample() is approximate, so report what actually came back
    # rather than what was asked for - the caller divides by this.
    realized = (len(sample_pdf) / total) if total else 1.0
    logger.info("Spark direct ingestion: pandas sample is %d rows (%.4f of the file).",
                len(sample_pdf), realized)

    # The full cached frame is returned: each ML stage caps its own input,
    # because what a valid sample looks like differs by stage (see
    # _spark_forecasting and _spark_segmentation).
    return sdf, sample_pdf, time_col, total, (realized or 1.0)


def spark_etl(pdf: pd.DataFrame):
    """Spark ingestion + cleaning. Returns (sdf ready for ML, cleaned pandas
    df, time_col). Raises on Spark failure - caller falls back to pandas.

    The returned frame is the whole cleaned dataset; each ML stage caps its
    own input, since a valid sample for training is not a valid sample for
    per-customer aggregation.
    """
    spark = _get_spark()

    pdf, time_col = _prep_pandas(pdf)
    sdf = spark.createDataFrame(pdf)          # ingestion into Spark
    sdf = _spark_clean(sdf).cache()
    logger.info("Spark ETL complete: %d rows after cleaning.", sdf.count())

    return sdf, sdf.toPandas(), time_col


def spark_forecasting(sdf, time_col):
    return _spark_forecasting(sdf, time_col)


def spark_segmentation(sdf, time_col):
    return _spark_segmentation(sdf, time_col)


def _prep_pandas(pdf: pd.DataFrame):
    """Prepare the raw pandas frame for Spark: sanitize column names,
    parse the first date-like column, stabilize object column types."""
    pdf = pdf.copy()
    pdf.columns = _dedupe([re.sub(r"\W", "_", str(c)).strip("_") or "col" for c in pdf.columns])

    time_col = None
    for col in pdf.columns:
        if pd.api.types.is_datetime64_any_dtype(pdf[col]):
            time_col = col
            break
    if time_col is None:
        for col in pdf.select_dtypes(include="object").columns:
            # Probe a sample first so non-date text columns are rejected
            # cheaply; only the winning column gets a full parse.
            probe = pdf[col].dropna()
            if probe.empty:
                continue
            if len(probe) > policy.DATE_PROBE_ROWS:
                probe = probe.sample(policy.DATE_PROBE_ROWS, random_state=42)
            if pd.to_datetime(probe, errors="coerce").notna().mean() <= policy.DATE_PARSE_THRESHOLD:
                continue
            parsed = pd.to_datetime(pdf[col], errors="coerce")
            if parsed.notna().mean() > policy.DATE_PARSE_THRESHOLD:
                pdf[col] = parsed
                time_col = col
                break

    for col in pdf.select_dtypes(include="object").columns:
        pdf[col] = pdf[col].map(lambda v: None if pd.isna(v) else str(v))
    return pdf, time_col


def _compose_date(sdf, time_col):
    """Rebuild a timestamp from year/month/day columns when one is absent.

    Returns (sdf, time_col). The composed column exists so the chronological
    split has something to order by; it is a timestamp, so the numeric feature
    selection never picks it up, and the calendar features are not re-derived
    from it because the parts are already columns in their own right.
    """
    from pyspark.sql import functions as F

    parts = policy.pick_date_parts(sdf.columns)
    if parts is None:
        return sdf, time_col
    year, month, day = parts
    numeric = {c for c, t in sdf.dtypes if t in ("int", "bigint", "float", "double")}
    if not {year, month, day} <= numeric:
        return sdf, time_col
    composed = F.make_date(F.col(year).cast("int"),
                           F.col(month).cast("int"),
                           F.col(day).cast("int")).cast("timestamp")
    sdf = sdf.withColumn(policy.COMPOSED_DATE_COL, composed)
    non_null = sdf.filter(F.col(policy.COMPOSED_DATE_COL).isNotNull()).limit(1).count()
    if not non_null:
        logger.info("Date parts %s/%s/%s did not compose into a valid date; "
                    "leaving the frame without a time column.", year, month, day)
        return sdf.drop(policy.COMPOSED_DATE_COL), time_col
    logger.info("Composed a timestamp from '%s', '%s' and '%s' so the split is "
                "chronological rather than file order.", year, month, day)
    return sdf, policy.COMPOSED_DATE_COL


def _dedupe(names):
    seen, out = {}, []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


def _spark_clean(sdf):
    """Cleaning per methodology 3.2.1, executed as Spark jobs."""
    from pyspark.sql import functions as F

    sdf = sdf.dropDuplicates()

    # Validation rules (3.2.1), identical to the pandas path: a null is left
    # for the imputation step, an out-of-range value is dropped, and a rule
    # that would delete more than MAX_VALIDITY_DROP_FRACTION of the rows is
    # read as a state the column encodes rather than as an error.
    numeric_types = ("int", "bigint", "float", "double")
    ruled = [(c, policy.validity_rule(c)) for c, t in sdf.dtypes
             if t in numeric_types and policy.validity_rule(c)]
    if ruled:
        def _violation(col, rule):
            return (F.col(col) <= 0) if rule == "positive" else (F.col(col) < 0)

        # One pass for the row count and every rule's violations together.
        counts = sdf.select(
            F.count(F.lit(1)).alias("_total"),
            *[F.sum((_violation(c, r) & F.col(c).isNotNull()).cast("int")).alias(c)
              for c, r in ruled]
        ).first()
        n_rows = counts["_total"] or 0
        for col, rule in ruled:
            bad = counts[col] or 0
            if not bad:
                continue
            if not policy.rule_is_enforceable(bad, n_rows):
                logger.info(
                    "Spark cleaning: '%s' fails %s on %d of %d rows (%.1f%%); "
                    "keeping them as a state the column encodes.",
                    col, rule, bad, n_rows, 100 * bad / max(n_rows, 1))
                continue
            sdf = sdf.filter(~_violation(col, rule) | F.col(col).isNull())
            logger.info("Spark cleaning: removed %d rows failing %s on '%s'.",
                        bad, rule, col)

    # Identifiers are dropped, never imputed - see policy.never_impute.
    id_cols = [c for c in sdf.columns if policy.never_impute(c)]
    if id_cols:
        sdf = sdf.dropna(subset=id_cols)

    total = sdf.count()

    nulls = sdf.select(
        [F.sum(F.col(c).isNull().cast("int")).alias(c) for c in sdf.columns]
    ).first()
    keep = [c for c in sdf.columns
            if (nulls[c] or 0) <= total * (1.0 - policy.MIN_NON_NULL_FRACTION)]
    sdf = sdf.select(keep)

    numeric = [c for c, t in sdf.dtypes if t in ("int", "bigint", "float", "double")]
    strings = [c for c, t in sdf.dtypes if t == "string"]

    fills = {}
    for c in numeric:
        if (nulls[c] or 0) > 0:
            med = sdf.approxQuantile(c, [0.5], 0.01)
            if med:
                fills[c] = float(med[0])
    for c in strings:
        if (nulls[c] or 0) > 0:
            mode = (
                sdf.filter(F.col(c).isNotNull())
                .groupBy(c).count().orderBy(F.desc("count")).first()
            )
            fills[c] = mode[0] if mode else "unknown"
    return sdf.fillna(fills) if fills else sdf


# ═══════════════════════════════════════════════════════════════════════════
# DEMAND FORECASTING - Spark MLlib
# ═══════════════════════════════════════════════════════════════════════════

def _spark_forecasting(sdf, time_col):
    from pyspark.ml.evaluation import RegressionEvaluator
    from pyspark.ml.feature import StringIndexer, VectorAssembler
    from pyspark.ml.regression import GBTRegressor, LinearRegression, RandomForestRegressor
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    split_sdf = None
    try:
        numeric = [c for c, t in sdf.dtypes if t in ("int", "bigint", "float", "double")]
        if not numeric:
            return {"skipped": True, "skip_reason": "No numeric columns found."}

        # Row-sampling is sound here: each row is an independent training
        # example, so a uniform sample trains the same relationship.
        n_rows = sdf.count()
        if n_rows > ML_MAX_ROWS:
            sdf = sdf.sample(fraction=ML_MAX_ROWS / n_rows, seed=42)
            n_rows = sdf.count()
            logger.info("Spark forecasting: sampled to ~%d rows for training.", n_rows)

        # Target: strongest sales-like keyword, else highest variance
        target = policy.pick_target(numeric)
        if target is None:
            variances = sdf.select([F.variance(c).alias(c) for c in numeric]).first().asDict()
            target = max(variances, key=lambda c: variances[c] or 0)

        # Feature engineering: calendar features from the timestamp column.
        # Skipped for a composed timestamp - it was built from year/month/day
        # columns that are already features, so re-deriving them would only
        # duplicate what the model already has.
        if time_col and time_col != policy.COMPOSED_DATE_COL:
            sdf = (sdf
                   .withColumn("_month",   F.month(time_col))
                   .withColumn("_weekday", F.dayofweek(time_col))
                   .withColumn("_day",     F.dayofmonth(time_col)))

        # Encode low-cardinality string columns with StringIndexer.
        # One agg for every cardinality at once: asking per column costs a
        # separate Spark job each time.
        string_cols = [c for c, t in sdf.dtypes if t == "string"]
        if string_cols:
            distincts = sdf.agg(*[
                F.approx_count_distinct(c).alias(c) for c in string_cols
            ]).first().asDict()
            for c in string_cols:
                if policy.is_encodable(distincts[c] or 0, n_rows):
                    sdf = StringIndexer(
                        inputCol=c, outputCol=f"{c}_idx", handleInvalid="keep"
                    ).fit(sdf).transform(sdf)

        features = [c for c, t in sdf.dtypes
                    if t in ("int", "bigint", "float", "double") and c != target]
        if not features:
            return {"skipped": True, "skip_reason": "No usable feature columns besides the target."}

        sdf = sdf.dropna(subset=[target] + features)

        # Chronological split (no leakage): row_number ordered by time.
        # Cached because the window has no partition key, so every downstream
        # action would otherwise redo the full sort - once for the train
        # filter, once for the test filter, and again per model.
        order = F.col(time_col) if time_col else F.monotonically_increasing_id()
        split_sdf = sdf.withColumn("_rn", F.row_number().over(Window.orderBy(order))).cache()
        n = split_sdf.count()
        if not policy.can_split(n):
            return {"skipped": True, "skip_reason": "Not enough rows to train and evaluate models."}
        split = policy.split_index(n)

        assembler = VectorAssembler(inputCols=features, outputCol="_features")
        train = assembler.transform(split_sdf.filter(F.col("_rn") <= split))
        test  = assembler.transform(split_sdf.filter(F.col("_rn") > split))

        models = {
            "Linear Regression": LinearRegression(featuresCol="_features", labelCol=target),
            "Random Forest": RandomForestRegressor(
                featuresCol="_features", labelCol=target,
                numTrees=50, maxDepth=10, maxBins=128, seed=42),
            "Gradient Boosting": GBTRegressor(
                featuresCol="_features", labelCol=target,
                maxIter=50, maxDepth=5, maxBins=128, seed=42),
        }

        metrics, importances = {}, {}
        for name, est in models.items():
            try:
                model = est.fit(train)
                preds = model.transform(test)
                m = {}
                for metric in ("rmse", "mae", "r2"):
                    ev = RegressionEvaluator(
                        labelCol=target, predictionCol="prediction", metricName=metric)
                    m[metric] = round(float(ev.evaluate(preds)), 4)
                metrics[name] = m
                logger.info("MLlib %s: RMSE=%s MAE=%s R2=%s", name, m["rmse"], m["mae"], m["r2"])
                if name == "Random Forest":
                    imp = dict(zip(features, model.featureImportances.toArray()))
                    importances = {
                        k: round(float(v), 4)
                        for k, v in sorted(imp.items(), key=lambda x: -x[1])[:10]
                    }
            except Exception as exc:
                logger.warning("MLlib %s failed: %s", name, exc)

        if not metrics:
            return {"skipped": True, "skip_reason": "All MLlib models failed to train."}

        return {
            "skipped": False,
            "skip_reason": None,
            "target_column": target,
            "time_column": time_col,
            "n_features": len(features),
            "train_rows": split,
            "test_rows": n - split,
            "models": metrics,
            "best_model": min(metrics, key=lambda k: metrics[k]["rmse"]),
            "feature_importance": importances,
        }
    except Exception as exc:
        logger.warning("Spark forecasting error (non-fatal): %s", exc)
        return {"skipped": True, "skip_reason": str(exc)}
    finally:
        if split_sdf is not None:
            split_sdf.unpersist()


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER SEGMENTATION - Spark MLlib K-Means on RFM
# ═══════════════════════════════════════════════════════════════════════════

def _spark_segmentation(sdf, time_col):
    from pyspark.ml.clustering import KMeans
    from pyspark.ml.evaluation import ClusteringEvaluator
    from pyspark.ml.feature import StandardScaler, VectorAssembler
    from pyspark.sql import functions as F

    try:
        # RFM is aggregated from the whole frame, never from a row sample:
        # every RFM feature sums over a customer's full history, so dropping
        # transactions first would deflate frequency and monetary by the
        # sampling fraction and bias recency late. The aggregate is one row
        # per customer, so it is small by construction.
        feats, method = _spark_rfm(sdf, time_col)
        if feats is not None:
            # Signed log transform (3.3.0): frequency/monetary are heavily
            # skewed - without this a single whale customer becomes its own
            # cluster and inflates the silhouette score. Clustering uses the
            # transformed values; profiles report the raw ones.
            for c in ("frequency", "monetary"):
                feats = feats.withColumn(
                    f"_{c}_log", F.signum(F.col(c)) * F.log1p(F.abs(F.col(c))))
            cluster_cols = ["recency", "_frequency_log", "_monetary_log"]
            profile_cols = ["recency", "frequency", "monetary"]
        else:
            # No RFM: rows are the entities, so row-sampling is the right cap.
            n = sdf.count()
            if n > ML_MAX_ROWS:
                sdf = sdf.sample(fraction=ML_MAX_ROWS / n, seed=42)
                n = sdf.count()
                logger.info("Spark segmentation: sampled to ~%d rows.", n)

            numeric = [c for c, t in sdf.dtypes if t in ("int", "bigint", "float", "double")]
            # One agg for all cardinalities: per-column asks cost a job each
            distincts = (sdf.agg(*[F.approx_count_distinct(c).alias(c) for c in numeric])
                         .first().asDict()) if numeric else {}
            usable = [c for c in numeric
                      if not policy.is_identifier(distincts[c] or 0, n)]
            if len(usable) < 2 or n < policy.MIN_CLUSTER_ROWS:
                return {"skipped": True,
                        "skip_reason": "No RFM columns detected and not enough numeric features for clustering."}
            feats = sdf.select(usable[:policy.MAX_CLUSTER_FEATURES]).dropna()
            method = "Spark MLlib K-Means on numeric features (no customer/date columns found)"
            cluster_cols = profile_cols = feats.columns

        assembled = VectorAssembler(inputCols=cluster_cols, outputCol="_vec").transform(feats)
        scaled = (StandardScaler(inputCol="_vec", outputCol="_scaled", withMean=True, withStd=True)
                  .fit(assembled).transform(assembled).cache())
        n_entities = scaled.count()

        evaluator = ClusteringEvaluator(featuresCol="_scaled", predictionCol="_seg")
        best = {"k": None, "score": -1.0, "preds": None}
        for k in policy.k_candidates(n_entities):
            # Restart K-Means from several seeds and keep the best, the way
            # scikit-learn's n_init does on the pandas path. K-Means converges
            # to a local optimum, and MLlib draws its starting centroids within
            # data partitions - so with a single start the answer depends on
            # how Spark happened to partition the input, and a run on a
            # different executor count can select a different k entirely.
            for seed in policy.kmeans_seeds():
                km = KMeans(k=k, seed=seed, featuresCol="_scaled", predictionCol="_seg")
                preds = km.fit(scaled).transform(scaled)
                score = float(evaluator.evaluate(preds))
                logger.info("MLlib K-Means k=%d seed=%d silhouette=%.4f", k, seed, score)
                if score > best["score"]:
                    best = {"k": k, "score": score, "preds": preds}

        profiles = []
        rows = (best["preds"].groupBy("_seg")
                .agg(F.count("*").alias("_n"), *[F.avg(c).alias(c) for c in profile_cols])
                .orderBy("_seg").collect())
        for r in rows:
            profiles.append({
                "segment": int(r["_seg"]),
                "size": int(r["_n"]),
                "profile": {c: round(float(r[c]), 2) for c in profile_cols},
            })
        scaled.unpersist()

        return {
            "skipped": False,
            "skip_reason": None,
            "method": method,
            "k": int(best["k"]),
            "silhouette": round(best["score"], 4),
            "n_entities": n_entities,
            "segments": profiles,
        }
    except Exception as exc:
        logger.warning("Spark segmentation error (non-fatal): %s", exc)
        return {"skipped": True, "skip_reason": str(exc)}


def _spark_rfm(sdf, time_col):
    """Aggregate to RFM per customer via Spark groupBy, or (None, None)."""
    from pyspark.sql import functions as F

    cust = policy.pick_customer_column(sdf.columns)
    if cust is None or time_col is None:
        return None, None

    numeric = [c for c, t in sdf.dtypes if t in ("int", "bigint", "float", "double")]
    choice = policy.pick_monetary(numeric)
    if choice is None:
        return None, None
    monetary = F.col(choice[1]) if choice[0] == "direct" else F.col(choice[1]) * F.col(choice[2])

    max_date = sdf.agg(F.max(time_col)).first()[0]
    if max_date is None:
        return None, None

    rfm = (sdf.filter(F.col(cust).isNotNull())
           .groupBy(cust)
           .agg(
               F.datediff(F.lit(max_date), F.max(time_col)).alias("recency"),
               F.count("*").alias("frequency"),
               F.sum(monetary).alias("monetary"),
           )
           .dropna()
           .drop(cust))
    n_customers = rfm.count()
    if n_customers < policy.MIN_RFM_CUSTOMERS:
        return None, None
    if n_customers > ML_MAX_ROWS:
        # Cap customers, never transactions: each row here is already one
        # customer's whole history, so sampling now costs no fidelity.
        rfm = rfm.sample(fraction=ML_MAX_ROWS / n_customers, seed=42)
        logger.info("Spark segmentation: sampled to ~%d customers.", ML_MAX_ROWS)
    return rfm, "Spark MLlib K-Means on RFM (Recency/Frequency/Monetary, log-scaled)"
