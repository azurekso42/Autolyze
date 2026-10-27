# Autolyze

A big data analytics framework for retail sales forecasting and customer insight.

Capstone Project 2 - BSc (Hons) Information Systems (Data Analytics), Sunway University.

Autolyze ingests retail transaction data, forecasts demand, segments customers, and
explains the computed results in plain business language. Its defining property is a
strict separation between computation and interpretation: every number is produced by
the Python and Apache Spark engine, and the language model is given those numbers and
prohibited from calculating anything itself.

---

## What it does

- **Distributed ETL**: Apache Spark cleans and unifies multi-table retail data.
  The M5 source tables combine into 59,181,090 item-day records in ~22 seconds.
- **Demand forecasting**: Linear Regression, Random Forest and Gradient Boosting,
  trained under a chronological split with RMSE, MAE and R² reported for each.
- **Customer segmentation**: K-Means over log-scaled Recency, Frequency and Monetary
  features, with *k* chosen by the Silhouette Coefficient.
- **Forward projection**: recursive gradient boosting over lag, rolling-mean and
  calendar features, rendered as a visually distinct continuation of the observed series.
- **Interpretation layer**: a locally hosted LLM turns the computed metrics into
  business-readable insights. It never sees the dataset, only the results.
- **Export**: all computed results as a multi-sheet Excel workbook that Power BI imports.

Two execution engines implement the same methodology - Apache Spark and
pandas/scikit-learn - so each acts as an independent check on the other, and the cost
of distributed processing can be measured rather than assumed.

---

## Requirements

| | |
|---|---|
| Python | 3.11+ |
| Java | JDK 17+ (Apache Spark depends on it) |
| Local LLM server | any OpenAI-compatible endpoint, e.g. LM Studio |

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows
pip install -r backend/requirements.txt
```

Configure credentials:

```bash
copy .env.example .env
```

Then fill in `.env`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                                  # SECRET_KEY
python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"  # password hash
```

Start the local LLM server and load an instruction-tuned model
(`qwen2.5-14b-instruct` was used for the reported results).

Run:

```bash
.\start_autolyze.ps1
```

Then open <http://127.0.0.1:8000>.

---

## Datasets

Not included in this repository - download them directly:

| Dataset | Source | Used for |
|---|---|---|
| M5 Forecasting | Zenodo `10.5281/zenodo.10203108` | demand forecasting |
| Online Retail II | UCI Machine Learning Repository | customer segmentation |

To build the unified M5 table from the three source files:

```bash
python scripts/prepare_m5.py "path/to/m5/files"
```

This unpivots 1,941 day columns and joins the calendar and weekly price tables,
producing ~59.2M records and a daily store-category aggregate for analysis.

---

## Tests

```bash
python -m pytest -q
```

50 tests covering the pipeline's methodological guarantees: that the train/test
split is chronological rather than random, that skewed monetary values do not
produce a singleton cluster, that segment profiles are reported in original
units, that RFM is aggregated over each customer's whole history rather than a
row sample, that totals estimated from a sample are scaled and labelled as
estimates, and that each stage degrades with a stated reason rather than
failing when a dataset cannot support it.

The 46 tests in `backend/tests/test_pipeline.py` need no Java, Spark or LLM and
run in seconds. The 12 in `backend/tests/test_spark_engine.py` exercise the
distributed path against real Spark and skip automatically when no JDK is
present, so the same command works either way.

---

## Reproducing the report figures

```bash
python report_figures/make_figures.py
```

Reads `results.json` and `bigscale.json` - the recorded output of the evaluation runs -
and regenerates the result figures. No values are hardcoded. The PNGs themselves are not
committed: they are build output, and the copies that matter are embedded in the report.

---

## Layout

```
backend/
  api/          FastAPI application, routing, rate limiting
  auth/         bcrypt credentials, JWT access and refresh tokens
  services/     analysis pipeline, Spark engine, LLM client, export
  utils/        upload handling, logging
  models/       response schemas
frontend/       single-page dashboard
report_figures/ figure generation and the measurement data behind it
scripts/        M5 dataset preparation
```

---

## Notes

- Spark runs in `local[*]` mode; no cluster is required.
- Datasets below a configurable row threshold are routed to the single-machine engine,
  because distributed overhead is not recovered at small sizes. Spark's value here is
  memory-bounded capacity - a 3.8 GB file is processed within a 4 GB driver - rather
  than raw speed.
- Nothing leaves the machine. The LLM is local, so transaction data is never sent to a
  third-party service.
