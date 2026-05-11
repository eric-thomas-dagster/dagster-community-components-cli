# GCP Observability Snapshot — Cloud Logging + Cloud Monitoring → BigQuery

**Validated end-to-end against real APIs** (servicepulse-490502). Pulls
recent errors from Cloud Logging and recent time-series from Cloud
Monitoring, lands both in BigQuery for ad-hoc SQL across log events
and metric points.

```
recent_errors          ← cloud_logging_query_asset    (severity>=ERROR, 24h)
       └── errors_flat ← dataframe_flatten_nested_columns
                 └── errors_bq        ← dataframe_to_bigquery

api_call_metrics       ← cloud_monitoring_metrics_asset (api/request_count, 1h)
       └── metrics_flat ← dataframe_flatten_nested_columns
                 └── metrics_bq       ← dataframe_to_bigquery
```

## Components covered (4)

| Component | What it does |
|---|---|
| `cloud_logging_query_asset` | Run a [Cloud Logging filter](https://cloud.google.com/logging/docs/view/logging-query-language) and return matching entries as a DataFrame (timestamp, severity, log_name, resource_*, payloads, labels). |
| `cloud_monitoring_metrics_asset` | Query Cloud Monitoring's ListTimeSeries API. Per-row: metric_type, labels, value, time-bucket. Supports aligners + cross-series reducers. |
| `dataframe_flatten_nested_columns` | JSON-stringify dict/list columns. Required between the Cloud Logging / Cloud Monitoring sources (which emit nested `resource_labels`, `metric_labels`, etc.) and `dataframe_to_bigquery` (which can't infer schemas for nested object-dtype columns). |
| `dataframe_to_bigquery` | Load any DataFrame into a BigQuery table. |

## Live run output

| Asset | Rows materialized |
|---|---|
| `recent_errors` | 6 (real BQ permission-denied audit entries from the project) |
| `api_call_metrics` | 9 series × ~5-min buckets = 9 points |

BigQuery tables created:
- `servicepulse-490502.dagster_demo.gcp_observability_errors`
- `servicepulse-490502.dagster_demo.gcp_observability_api_metrics`

## Cost

**Free.** Cloud Logging reads are free up to 50 GB/mo, Monitoring metric reads are free, BQ loads at this volume are well under the free tier.

## Bugs surfaced and fixed validating this demo

1. **`google.cloud.monitoring_v3` has no `MetricDescriptor` attribute** at the top level. Original code did `monitoring_v3.MetricDescriptor.ValueType(...)`. Replaced with an inline `_VALUE_TYPE_NAMES` dict (the proto enum is stable: BOOL/INT64/DOUBLE/STRING/DISTRIBUTION/MONEY).
2. **Time interval start_time/end_time aren't Timestamp protos in current API** — they're `DatetimeWithNanoseconds` objects already. `.ToDatetime()` blew up. Added a `_ts_to_datetime()` helper that handles both shapes.
3. **`dataframe_to_bigquery` rejects dict/list columns** with `Empty schema specified for the load job`. Cloud Logging entries (`resource_labels`, `json_payload`, `labels`) and Monitoring points (`metric_labels`, `resource_labels`) emit dicts. Added a pandas flatten step between source and sink that JSON-stringifies any column containing dicts/lists. Keeps the sources rich + the sink generic.
4. **Class name is `DataframeToBigqueryComponent` (lowercase q)** — not `DataframeToBigQueryComponent`. Worth knowing when writing YAML by hand.

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
export BQ_DATASET=your_dataset
```

## Required APIs

| API | Enable URL |
|---|---|
| Cloud Logging | https://console.cloud.google.com/apis/library/logging.googleapis.com |
| Cloud Monitoring | https://console.cloud.google.com/apis/library/monitoring.googleapis.com |
| BigQuery | https://console.cloud.google.com/apis/library/bigquery.googleapis.com |

## Required IAM (on the service account)

- `roles/logging.viewer`
- `roles/monitoring.viewer`
- `roles/bigquery.dataEditor` on `$BQ_DATASET`
- `roles/bigquery.jobUser` (project-level)

## Run it

```bash
./setup_gcp_observability_snapshot_demo.sh
cd gcp-observability-snapshot-demo
uv run dg launch --assets '*'
```

## What you can do downstream

- **Daily error roll-ups**: schedule the pipeline daily, then point a BI tool at `gcp_observability_errors`.
- **API-spend anomaly detection**: pull `api_call_metrics` historically and run a control chart over `value` per `resource_labels.method`.
- **SLO compliance**: swap the monitoring filter to `metric.type="run.googleapis.com/request_latencies"` for Cloud Run p95 latency snapshots.
- **Cross-project audit**: change `resource_names: [folders/<id>]` on `cloud_logging_query_asset` to pull errors across every project in a folder.

## Sister components (planned wave 5)

- `cloud_dlp_inspect_asset` — scan a DataFrame column for PII via Cloud DLP
- `cloud_composer_trigger_asset` — kick off an Airflow DAG running on Composer
- `bigtable_reader_asset` / `bigtable_writer_asset` — wide-column NoSQL
- `looker_query_asset` — read Looker Modeled SQL outputs
- `cloud_run_service_invoke_asset` — synchronous Cloud Run service request
