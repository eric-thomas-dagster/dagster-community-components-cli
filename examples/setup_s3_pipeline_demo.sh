#!/usr/bin/env bash
# S3 dynamic-partition pipeline demo — sensor-driven, 100% components.
#
# Drops a few CSVs into a Minio-backed S3 bucket, points an `s3_monitor`
# sensor at it (in dynamic-partition mode), and watches it register a
# partition per new file and trigger a downstream `file_ingestion` →
# `summarize` → `dataframe_to_parquet` pipeline that writes back to S3.
#
# Why Minio: real S3 API surface, no AWS credentials needed, no cost.
# Auth uses ambient env vars (AWS_ACCESS_KEY_ID/SECRET) — fsspec/s3fs
# auto-discover them.
#
# Pipeline (5 components, all autoloaded by `dg`):
#
#   [Minio S3 bucket: my-data/incoming/*.csv]
#                  │
#                  ▼
#   s3_monitor (partition_mode: dynamic_partition)
#                  │   registers each new key as a partition,
#                  │   yields RunRequest(partition_key=...)
#                  ▼
#   file_ingestion (partition_type: dynamic + from_run_config uri_template)
#                  │   reads s3://my-data/<partition_key>  (auto-detect CSV)
#                  ▼
#   summarize    (per-region aggregations)
#                  │
#                  ▼
#   dataframe_to_parquet (writes back to s3://my-data/processed/...)

set -euo pipefail

PROJECT_DIR="${1:-s3-pipeline-demo}"
MINIO_NAME=dg-s3-demo-minio
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001
BUCKET=my-data
AWS_KEY=minioadmin
AWS_SECRET=minioadmin

echo ">>> 1/6  Starting Minio in Docker ($MINIO_NAME:$MINIO_PORT, console:$MINIO_CONSOLE_PORT)"
docker rm -f "$MINIO_NAME" >/dev/null 2>&1 || true
docker run -d --name "$MINIO_NAME" \
  -p $MINIO_PORT:9000 -p $MINIO_CONSOLE_PORT:9001 \
  -e MINIO_ROOT_USER=$AWS_KEY -e MINIO_ROOT_PASSWORD=$AWS_SECRET \
  minio/minio server /data --console-address ":9001" >/dev/null
sleep 2
echo "    Minio running. Console: http://localhost:$MINIO_CONSOLE_PORT  (user: $AWS_KEY / pass: $AWS_SECRET)"

echo ">>> 2/6  Creating bucket + seeding 3 sample CSVs into s3://$BUCKET/incoming/"
docker run --rm --network host \
  -e AWS_ACCESS_KEY_ID=$AWS_KEY -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET \
  --entrypoint sh amazon/aws-cli:latest -c "
    aws --endpoint-url http://localhost:$MINIO_PORT s3 mb s3://$BUCKET 2>/dev/null || true
    cat > /tmp/sales_us.csv <<CSV
region,product,units,revenue
us-west,widget,12,1200
us-west,gizmo,8,800
us-east,widget,15,1500
us-east,gizmo,11,1100
CSV
    cat > /tmp/sales_eu.csv <<CSV
region,product,units,revenue
eu-fr,widget,9,900
eu-fr,gizmo,14,1400
eu-de,widget,7,700
eu-de,gizmo,6,600
CSV
    cat > /tmp/sales_apac.csv <<CSV
region,product,units,revenue
apac-jp,widget,20,2000
apac-jp,gizmo,18,1800
apac-au,widget,5,500
apac-au,gizmo,3,300
CSV
    aws --endpoint-url http://localhost:$MINIO_PORT s3 cp /tmp/sales_us.csv   s3://$BUCKET/incoming/sales_us.csv
    aws --endpoint-url http://localhost:$MINIO_PORT s3 cp /tmp/sales_eu.csv   s3://$BUCKET/incoming/sales_eu.csv
    aws --endpoint-url http://localhost:$MINIO_PORT s3 cp /tmp/sales_apac.csv s3://$BUCKET/incoming/sales_apac.csv
" >/dev/null
echo "    Seeded 3 CSVs."

echo ">>> 3/6  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
uv add -q pandas s3fs boto3 fsspec >/dev/null
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver >/dev/null

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/6  Installing 4 components"
$CLI add file_ingestion        --auto-install
$CLI add summarize             --auto-install
$CLI add dataframe_to_parquet  --auto-install
$CLI add s3_monitor            --auto-install

# Suppress auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/file_ingestion" "src/$PKG/defs/summarize" \
       "src/$PKG/defs/dataframe_to_parquet" "src/$PKG/defs/s3_monitor"

mkdir -p "src/$PKG/defs/sales_raw" "src/$PKG/defs/sales_by_product" \
         "src/$PKG/defs/sales_parquet" "src/$PKG/defs/s3_sensor"

echo ">>> 5/6  Writing defs.yaml — Minio endpoint via env vars"

# ---- 1. file_ingestion: dynamic-partition asset, reads URI from sensor-injected
#         partition key. The Minio endpoint is configured via env vars consumed
#         by s3fs/boto3.
cat > "src/$PKG/defs/sales_raw/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: sales_raw
  format: auto
  partition_type: dynamic
  dynamic_partition_name: "s3_keys"
  from_run_config:
    uri_template: "{partition_key}"
  description: "Per-file CSV ingestion from S3. Each detected key becomes a dynamic partition."
  group_name: ingest
EOF

# ---- 2. summarize: aggregate per product. ALSO partitioned the same way so
#       each S3 input file flows through end-to-end as its own partition.
cat > "src/$PKG/defs/sales_by_product/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: sales_by_product
  upstream_asset_key: sales_raw
  partition_type: dynamic
  dynamic_partition_name: "s3_keys"
  group_by: [product]
  aggregations:
    total_units:   {col: units, agg: sum}
    total_revenue: {col: revenue, agg: sum}
    n_rows:        {col: revenue, agg: count}
  group_name: transform
EOF

# ---- 3. parquet sink: write each processed partition back to S3 as its own
#       parquet file. `{partition_key}` templates the S3 key into file_path,
#       so 3 input CSVs → 3 distinct parquet outputs.
cat > "src/$PKG/defs/sales_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: sales_parquet
  upstream_asset_key: sales_by_product
  partition_type: dynamic
  dynamic_partition_name: "s3_keys"
  file_path: "s3://$BUCKET/processed/{partition_key}.parquet"
  compression: snappy
  group_name: sink
EOF

# ---- 4. s3_monitor: dynamic-partition mode
cat > "src/$PKG/defs/s3_sensor/defs.yaml" <<EOF
type: $PKG.components.s3_monitor.component.S3MonitorSensorComponent
attributes:
  sensor_name: sales_s3_sensor
  bucket_name: $BUCKET
  prefix: "incoming/"
  key_pattern: ".*\\\\.csv\$"
  job_name: __ASSET_JOB
  minimum_interval_seconds: 10
  partition_mode: dynamic_partition
  dynamic_partitions_name: "s3_keys"
  # Use the bare S3 key (without scheme) — Dagster's filesystem IO manager
  # can't store partition state for keys containing "://".
  partition_key_template: "{key}"
  default_status: running
EOF

cat <<MSG

>>> 6/6  Setup complete. Open the UI:

    cd $PROJECT_DIR
    export AWS_ACCESS_KEY_ID=$AWS_KEY
    export AWS_SECRET_ACCESS_KEY=$AWS_SECRET
    export AWS_ENDPOINT_URL=http://localhost:$MINIO_PORT
    export DAGSTER_HOME=\$HOME/.dagster_home_s3_demo
    mkdir -p \$DAGSTER_HOME
    uv run dg dev

What you'll see:
  - sales_s3_sensor (running) — scans s3://$BUCKET/incoming/ every 10s
  - On its first tick it registers 3 partitions (one per CSV in the bucket)
    and launches 3 RunRequests
  - Each run materializes sales_raw (one partition) → sales_by_product →
    sales_parquet (writes s3://$BUCKET/processed/sales_by_product.parquet)

Drop another CSV into the bucket while dg dev is running and watch a 4th
partition appear:

    docker run --rm --network host \\
      -e AWS_ACCESS_KEY_ID=$AWS_KEY -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET \\
      --entrypoint sh amazon/aws-cli:latest -c \\
      "echo 'region,product,units,revenue
new-rg,gizmo,1,100' > /tmp/x.csv && aws --endpoint-url http://localhost:$MINIO_PORT s3 cp /tmp/x.csv s3://$BUCKET/incoming/sales_new.csv"

Inspect what's been written back to Minio:

    docker run --rm --network host \\
      -e AWS_ACCESS_KEY_ID=$AWS_KEY -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET \\
      amazon/aws-cli:latest --endpoint-url http://localhost:$MINIO_PORT \\
      s3 ls s3://$BUCKET/processed/

Cleanup:
    docker rm -f $MINIO_NAME
MSG
