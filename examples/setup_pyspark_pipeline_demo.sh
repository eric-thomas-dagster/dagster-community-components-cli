#!/usr/bin/env bash
# PySpark Pipeline demo — Catalyst-optimized DataFrame chain in one asset.
#
# WHAT THIS DEMONSTRATES
#   pyspark_pipeline: multi-step PySpark DataFrame chain compiled into ONE
#   Catalyst logical plan, then executed (Spark's Tungsten engine). Predicate
#   pushdown to parquet, projection pruning, filter fusion, parallel
#   execution — all the optimizations that the per-asset transforms can't
#   give you since the asset boundary breaks the lazy chain.
#
#   Components exercised (3):
#     - synthetic_data_generator  Python-side seed
#     - dataframe_to_parquet      land seed as parquet on local FS
#     - pyspark_pipeline          ONE asset that reads parquet, runs a 4-op
#                                 chain (filter → with_columns → group_by →
#                                 sort), writes parquet — all planned by
#                                 Catalyst as one query
#
#   Spark runs in `local[*]` mode (one process, all CPU cores). Retarget at
#   a real cluster by changing `spark_config.spark.master`.
#
# REQUIRES: Java 17+ on PATH (PySpark needs a JVM). Most systems already
#           have this from other tools; verify with `java -version`.
# COST: $0 — local Spark, local filesystem.

set -euo pipefail
PROJECT_DIR="${1:-pyspark-pipeline-demo}"

if ! command -v java >/dev/null 2>&1; then
  echo "ERROR: java not found on PATH. PySpark requires Java 17+."
  echo "macOS:   brew install openjdk"
  echo "Ubuntu:  apt install -y openjdk-17-jdk"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow 'pyspark>=3.4.0'

CLI="uvx --from dagster-community-components-cli dagster-component"
for c in synthetic_data_generator dataframe_to_parquet pyspark_pipeline; do
  $CLI add $c --auto-install
done
for c in synthetic_data_generator dataframe_to_parquet pyspark_pipeline; do
  rm -rf "src/$PKG/defs/$c"
done

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders
  schema_type: orders
  row_count: 2000
  random_state: 42
  group_name: pyspark_pipeline_demo"

write_yaml "orders_parquet" "type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: orders_parquet
  upstream_asset_key: orders
  file_path: ./orders.parquet
  group_name: pyspark_pipeline_demo"

write_yaml "top_3_per_category_spark" "type: $PKG.components.pyspark_pipeline.component.PySparkPipelineComponent
attributes:
  asset_name: top_3_per_category_spark
  spark_config:
    spark.master: \"local[*]\"
    spark.driver.bindAddress: \"127.0.0.1\"
    spark.ui.showConsoleProgress: \"false\"
  spark_app_name: dagster-pyspark-pipeline-demo
  source:
    kind: parquet
    path: ./orders.parquet
  operations:
    - op: filter
      predicate: \"status = 'paid' AND total > 50\"
    - op: with_columns
      expressions:
        is_high_value: \"total > 500\"
    - op: group_by
      group_by: [category]
      aggregations:
        revenue:     {col: total,    agg: sum}
        order_count: {col: order_id, agg: count}
    - op: sort
      by: [revenue]
      descending: true
    - op: limit
      n: 3
  sink:
    kind: parquet
    path: ./top_3_per_category_spark
    mode: overwrite
  deps: [orders_parquet]
  group_name: pyspark_pipeline_demo"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

What you'll see:
  1. orders                        → 2000 rows in memory (Python)
  2. orders_parquet                → ./orders.parquet on disk
  3. top_3_per_category_spark      → Spark reads the parquet (Catalyst
                                     pushes the WHERE predicate to the
                                     reader so only matching row groups
                                     come off disk), runs 5 ops as ONE
                                     query plan, writes ./top_3_per_category_spark/

After the run:
  ls -la ./top_3_per_category_spark/   # Parquet part files written by Spark

Retargeting at a real cluster:
  Just change spark_config:
    spark.master: \"spark://master-host:7077\"            # standalone
    spark.master: \"yarn\"                                  # YARN
    spark.master: \"k8s://https://kubernetes:443\"          # Kubernetes
  And add cluster-specific configs (cluster_manager, deploy mode, executor
  memory/cores, etc.). The component itself is unchanged.
MSG
