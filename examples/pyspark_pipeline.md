# PySpark Pipeline — Catalyst-optimized chain in a single asset

Multi-step PySpark DataFrame chain compiled into ONE Catalyst logical plan, executed by Spark's Tungsten engine. Predicate pushdown to parquet, projection pruning, filter fusion, parallel execution — the optimizations the per-asset transforms can't deliver because the asset boundary breaks the lazy chain.

## When to use

- Big data that warrants Spark's distributed execution
- You want Catalyst's optimizer planning the whole sequence
- Source/sink is anything Spark can read/write (parquet, csv, json, delta, hive table, JDBC, …)
- Running on a Spark cluster (or `local[*]` for development/CI)

For small frames or single-machine work, `polars_pipeline` is lighter. For warehouse-side compute on Snowflake / BigQuery / etc., use `snowpark_pipeline` or the `warehouse_*` family.

## Components exercised (3)

- `synthetic_data_generator` — 2000-row Python-side seed
- `dataframe_to_parquet` — write seed to local parquet
- `pyspark_pipeline` — Spark reads parquet, runs 5-op chain (filter → with_columns → group_by → sort → limit), writes parquet

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pyspark_pipeline_demo.sh | bash
cd pyspark-pipeline-demo
uv run dg launch --assets '*'
```

## Requires

- Java 17+ on PATH (PySpark needs a JVM). Most systems already have this; verify with `java -version`. Install: `brew install openjdk` (macOS) / `apt install openjdk-17-jdk` (Ubuntu).

## Validated end-to-end

`local[*]` Spark, 2000 input rows → RUN_SUCCESS. Top-3 categories by revenue come out as parquet at `./top_3_per_category_spark/`. The 5-op chain ran as one Catalyst plan in ~6 seconds (includes JVM startup).

## Retargeting at a real cluster

Change `spark_config.spark.master`:

```yaml
spark_config:
  spark.master: "spark://master-host:7077"              # standalone
  spark.master: "yarn"                                   # YARN
  spark.master: "k8s://https://kubernetes.default:443"   # Kubernetes
  spark.master: "databricks-connect://..."               # Databricks Connect
```

Plus cluster-specific configs (deploy mode, executor memory/cores, driver bind, etc.). The component itself doesn't care which Spark deployment it's talking to.

## Sink kinds

| Kind | Behavior |
|---|---|
| `parquet` / `csv` / `json` / `orc` / `delta` | Spark writes to a path |
| `table` | Spark writes to a catalog table (Hive / Iceberg / Delta-as-table) |
| `jdbc` | Spark writes to a JDBC target — `dbtable` + auth options |
| `none` | Collects to pandas (the pandas DataFrame becomes the asset's output for downstream Dagster I/O) |

## Companion: snowpark_pipeline

For Snowflake-resident work, `snowpark_pipeline` is the same pattern with the same op vocabulary, but compiles to a single Snowflake SQL statement that runs entirely in Snowflake's compute warehouse. No data through Python at all.
