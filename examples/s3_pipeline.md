# S3 dynamic-partition pipeline — `setup_s3_pipeline_demo.sh`
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Sensor-driven round-trip pipeline, 100% components, validated end-to-end. New CSV files dropped into an S3 bucket are auto-detected, registered as dynamic partitions, processed individually, and written back to S3 as parquet — one partition per file, fully tracked and re-runnable from the UI.

## Components used

- `dataframe_to_parquet`
- `file_ingestion`
- `s3_monitor`
- `summarize`

## What gets stood up

```
┌────────────────────────────────────────────────────────────────────────┐
│  Local Docker: Minio (S3-compatible)  →  http://localhost:9001 console │
│  Bucket: my-data/                                                       │
│    incoming/   ← new CSVs dropped here trigger the pipeline             │
│    processed/  ← parquet outputs land here, one file per partition      │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
            ┌───────────────────────────────────────────────┐
            │  s3_monitor (partition_mode: dynamic_partition) │
            │   - polls s3://my-data/incoming/*.csv every 10s│
            │   - registers each new key as a dynamic partition
            │   - yields RunRequest(partition_key=incoming/sales_eu.csv) │
            └────────────────────┬──────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────────┐
              │  file_ingestion (partitioned, format=auto)│
              │    uri_template: s3://my-data/{partition_key}│
              │    → pd.read_csv(s3://...)                 │
              └────────────────────┬─────────────────────┘
                                   ▼
              ┌──────────────────────────────────────────┐
              │  summarize (partitioned)                  │
              │    group_by: [product]                    │
              │    agg: total_units, total_revenue, n_rows│
              └────────────────────┬─────────────────────┘
                                   ▼
              ┌──────────────────────────────────────────┐
              │  dataframe_to_parquet (partitioned)       │
              │    file_path: s3://my-data/processed/{partition_key}.parquet│
              └──────────────────────────────────────────┘
```

## Components (all from the community registry)

| # | Component | Role |
|---|---|---|
| 1 | `s3_monitor` | Polls S3 bucket, registers dynamic partitions, yields RunRequests |
| 2 | `file_ingestion` | Reads each CSV partition into a DataFrame (auto-detects format) |
| 3 | `summarize` | Aggregates per product within each partition |
| 4 | `dataframe_to_parquet` | Writes per-partition parquet back to S3 |

## Run

```bash
./setup_s3_pipeline_demo.sh
cd s3-pipeline-demo
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_ENDPOINT_URL=http://localhost:9000
export DAGSTER_HOME=$HOME/.dagster_home_s3_demo
mkdir -p $DAGSTER_HOME
uv run dg dev
```

Open <http://localhost:3000>. Within ~15 seconds the sensor fires, registers 3 partitions (one per seeded CSV), and launches 3 runs. Each run materializes:

1. `sales_raw[incoming/sales_us.csv]` — reads `s3://my-data/incoming/sales_us.csv` → DataFrame
2. `sales_by_product[incoming/sales_us.csv]` — aggregates per product
3. `sales_parquet[incoming/sales_us.csv]` — writes `s3://my-data/processed/incoming/sales_us.csv.parquet`

## The reprocessing story (the win)

Every detected file is now a **tracked, named partition**. From the Dagster UI:

- See all 3 partitions in the Assets view
- Re-materialize just `sales_apac.csv` with one click
- Backfill a range of partitions
- Inspect which partitions failed and retry them

This is the answer to "how do I track what files were processed and reprocess if needed" — dynamic partitions make every input file a first-class entity in your asset catalog.

## Drop a new file, watch a new partition appear

```bash
docker run --rm --network host \
  -e AWS_ACCESS_KEY_ID=minioadmin -e AWS_SECRET_ACCESS_KEY=minioadmin \
  --entrypoint sh amazon/aws-cli:latest -c \
  "echo 'region,product,units,revenue
new-rg,gizmo,1,100' > /tmp/x.csv && \
  aws --endpoint-url http://localhost:9000 s3 cp /tmp/x.csv s3://my-data/incoming/sales_new.csv"
```

Within 10s the sensor logs `Found 1 new S3 object(s) ... (partition_mode=dynamic_partition)`, registers `incoming/sales_new.csv` as a new partition, launches the run, and writes `s3://my-data/processed/incoming/sales_new.csv.parquet`.

## Inspect the outputs

```bash
# List parquet files in Minio
docker run --rm --network host \
  -e AWS_ACCESS_KEY_ID=minioadmin -e AWS_SECRET_ACCESS_KEY=minioadmin \
  amazon/aws-cli:latest --endpoint-url http://localhost:9000 \
  s3 ls --recursive s3://my-data/processed/

# Read one parquet via pandas
cd s3-pipeline-demo
uv run python -c "
import pandas as pd
df = pd.read_parquet('s3://my-data/processed/incoming/sales_us.csv.parquet')
print(df)
"
```

## Validated end-to-end

- ✓ Minio starts in Docker, bucket created, 3 CSVs seeded
- ✓ Sensor detects 3 new objects, registers 3 dynamic partitions
- ✓ 3 sensor-launched runs complete with status SUCCESS
- ✓ 3 distinct parquet files materialized to `s3://my-data/processed/`
- ✓ Adding a 4th CSV during `dg dev` triggers a 4th partition + run

## Swap Minio for real S3 / GCS / ADLS

The component code is storage-agnostic — only the URI scheme changes:

```yaml
# S3 (production AWS, IRSA / instance role / env vars)
url:        s3://my-prod-bucket/incoming/         # in the sensor
file_path:  s3://my-prod-bucket/processed/{partition_key}.parquet

# GCS
bucket_name: my-prod-bucket    # in gcs_monitor
file_path:   gs://my-prod-bucket/processed/{partition_key}.parquet

# ADLS Gen2
file_path:   abfss://container@account.dfs.core.windows.net/processed/{partition_key}.parquet
```

The same `file_ingestion` + `dataframe_to_parquet` components work — pandas + fsspec handle all three transparently when the right driver is installed (`s3fs` / `gcsfs` / `adlfs`).

## Teardown

```bash
docker rm -f dg-s3-demo-minio
rm -rf s3-pipeline-demo $HOME/.dagster_home_s3_demo
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
