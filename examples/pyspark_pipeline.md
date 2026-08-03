# PySpark Pipeline — Catalyst-optimized chain in a single asset

Multi-step PySpark DataFrame chain compiled into ONE Catalyst logical plan, executed by Spark's Tungsten engine. Predicate pushdown to parquet, projection pruning, filter fusion, parallel execution — the optimizations the per-asset transforms can't deliver because the asset boundary breaks the lazy chain.

## Components used

- `dataframe_to_parquet`
- `pyspark_pipeline`
- `synthetic_data_generator`

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

## Component reference

### pyspark_pipeline

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Output Dagster asset name |
| `spark_config` | dict[string, any] | no | SparkConf options. Values stringified before `.config()`. Set `spark.master` here for non-local clusters |
| `spark_app_name` | string | no | Spark application name. Default `dagster-pyspark-pipeline` |
| `source` | dict | yes | `{kind: parquet\|csv\|json\|orc\|delta\|table\|jdbc\|upstream, ...}` |
| `operations` | list[dict] | yes | Ordered PySpark DataFrame ops applied as one Catalyst plan |
| `sink` | dict | yes | `{kind: parquet\|csv\|json\|delta\|table\|jdbc\|none, ...}` |
| `upstream_asset_key` | string | conditional | Required when `source.kind: upstream`. Pandas/polars DataFrame is converted via `spark.createDataFrame` |
| `group_name`, `description`, `asset_tags`, `kinds`, `owners`, `deps` | (standard) | no | Standard Dagster metadata fields |

`source` kinds:

| `source.kind` | Required fields | Notes |
|---|---|---|
| `parquet` / `csv` / `json` / `orc` | `path` | Globs supported. Cloud URIs via Hadoop FS (s3a:// / gs:// / abfs://) need the right Hadoop bundles |
| `delta` | `path` | Requires `delta-spark` package + Delta Lake Spark config |
| `table` | `table` | Reads from Spark catalog (Hive metastore / Iceberg / Delta-as-table) |
| `jdbc` | `url`, `dbtable` OR `query`, optional `options:` | Predicate is pushed to the source DB |
| `upstream` | (also set top-level `upstream_asset_key`) | Accept pandas/polars upstream DataFrame |

`sink` kinds:

| `sink.kind` | Required fields | Behavior |
|---|---|---|
| `parquet` / `csv` / `json` / `delta` | `path`, optional `mode` | Spark writes to path |
| `table` | `table`, optional `mode` | Writes to Spark catalog |
| `jdbc` | `url`, `dbtable`, optional `options:`, `mode` | Writes to JDBC target |
| `none` | (none) | Collects to pandas; the pandas DataFrame becomes the asset's return value |

Supported `operations[*].op` values:

| `op` | Params | Notes |
|---|---|---|
| `filter` | `predicate: "<SQL>"` | SparkSQL predicate — pushes to source readers when possible |
| `select` | `columns: [a, b]` | DataFrame projection |
| `drop` | `columns: [a, b]` | Drop columns |
| `rename` | `mapping: {old: new}` | Rename columns |
| `with_columns` | `expressions: {name: <SparkSQL expr>}` | Add/replace via `F.expr(...)` |
| `group_by` | `group_by: [cols]`, `aggregations: {out: {col, agg}}` | groupBy + agg |
| `sort` | `by: [cols]`, `descending: bool/list` | orderBy |
| `limit` | `n: int` | `.limit(n)` |
| `distinct` | (none) | `.distinct()` |
| `drop_nulls` | `subset: [cols]` (optional) | `.dropna(subset=...)` |

Supported `agg` values in `group_by`: `sum / mean / avg / min / max / count / countDistinct / first / last / stddev / variance`.

### Component README (full reference)

[pyspark_pipeline](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/pyspark_pipeline/README.md)

## Demo notes

- **The whole chain is ONE Catalyst logical plan.** Catalyst sees the entire op sequence (read → filter → with_columns → group_by → sort → limit → write) before it executes, so it can do predicate pushdown to the parquet reader, projection pruning (columns not used downstream never get materialized), and join reordering. This is the optimization the per-asset transforms can't deliver.
- **`local[*]` for the demo.** The setup script uses `spark.master: "local[*]"` so one JVM does everything on your laptop's cores. For a real Spark cluster, change `spark.master` to your cluster URL (`spark://`, `yarn`, `k8s://...`, `databricks-connect://...`).
- **JVM startup overhead is real.** The demo's ~6 seconds is mostly JVM boot. For a real cluster, that's amortized across the cluster manager — submission overhead is similar in absolute terms.
- **`sink.kind: none` returns pandas.** Useful when downstream Dagster assets are pandas-shaped and the Spark output is small enough to collect. For large results, write to a sink kind (parquet / table / jdbc).

## See also

<!-- TODO: link related walkthroughs -->
