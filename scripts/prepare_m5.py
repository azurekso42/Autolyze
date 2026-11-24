"""Build the unified M5 tables Autolyze analyses.

Usage:
    python scripts/prepare_m5.py "path/to/m5/files" [output_dir]

Expects the three M5 source files in the input folder:
    sales_train_evaluation.csv   one row per item, columns d_1 ... d_1941
    calendar.csv                 day index -> date, weekday, month, year, SNAP flags
    sell_prices.csv              weekly price per store and item

Writes to output_dir (default: data/):
    m5_unified.parquet/          one row per item-day (~59.2M rows)
    m5_daily_store_category.csv  daily totals per store and category (~58K rows),
                                 the file uploaded to Autolyze for forecasting

Needs Java 17+ for Spark.
"""

from __future__ import annotations

import os
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

SALES_FILE = "sales_train_evaluation.csv"
CALENDAR_FILE = "calendar.csv"
PRICES_FILE = "sell_prices.csv"


def main(src: str, out: str) -> None:
    for name in (SALES_FILE, CALENDAR_FILE, PRICES_FILE):
        if not os.path.isfile(os.path.join(src, name)):
            sys.exit(f"Missing {name} in {src}")
    os.makedirs(out, exist_ok=True)

    spark = (
        SparkSession.builder.appName("prepare_m5")
        .master("local[*]")
        .config("spark.driver.memory", "4g")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    def read(name):
        return spark.read.csv(os.path.join(src, name), header=True, inferSchema=True)

    sales = read(SALES_FILE)
    calendar = read(CALENDAR_FILE)
    prices = read(PRICES_FILE)

    # Unpivot the d_1 ... d_N day columns into one row per item-day.
    id_cols = ["item_id", "dept_id", "cat_id", "store_id", "state_id"]
    day_cols = [c for c in sales.columns if c.startswith("d_")]
    print(f"Unpivoting {len(day_cols)} day columns for {sales.count():,} items...")
    long = sales.unpivot(id_cols, day_cols, "d", "units")

    # SNAP applies per state: pick the flag for the item's own state.
    cal = calendar.select(
        "d", "wm_yr_wk", F.to_date("date").alias("date"),
        "weekday", "month", "year", "snap_CA", "snap_TX", "snap_WI",
    )
    unified = (
        long.join(F.broadcast(cal), "d")
        .join(prices, ["store_id", "item_id", "wm_yr_wk"], "left")
        .withColumn(
            "snap",
            F.when(F.col("state_id") == "CA", F.col("snap_CA"))
            .when(F.col("state_id") == "TX", F.col("snap_TX"))
            .otherwise(F.col("snap_WI")),
        )
        .withColumn("revenue", F.col("units") * F.col("sell_price"))
        .drop("snap_CA", "snap_TX", "snap_WI", "wm_yr_wk", "d")
    )

    unified_path = os.path.join(out, "m5_unified.parquet")
    unified.write.mode("overwrite").parquet(unified_path)
    unified = spark.read.parquet(unified_path)
    print(f"Unified item-day records: {unified.count():,} -> {unified_path}")

    daily = (
        unified.groupBy("date", "store_id", "state_id", "cat_id")
        .agg(
            F.sum("units").alias("UnitsSold"),
            F.round(F.sum("revenue"), 2).alias("Revenue"),
            F.round(F.avg("sell_price"), 2).alias("AvgSellPrice"),
            F.first("weekday").alias("Weekday"),
            F.first("month").alias("Month"),
            F.first("year").alias("Year"),
            F.first("snap").alias("SNAP"),
        )
        .withColumnRenamed("date", "Date")
        .withColumnRenamed("store_id", "Store")
        .withColumnRenamed("state_id", "State")
        .withColumnRenamed("cat_id", "Category")
        .orderBy("Date", "Store", "Category")
    )

    daily_path = os.path.join(out, "m5_daily_store_category.csv")
    daily_pdf = daily.toPandas()  # ~58K rows, small enough for one file
    daily_pdf.to_csv(daily_path, index=False)
    print(f"Daily store-category aggregate: {len(daily_pdf):,} rows -> {daily_path}")

    spark.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "data")
