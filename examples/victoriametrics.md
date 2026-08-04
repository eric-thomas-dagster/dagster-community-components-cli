# VictoriaMetrics — DataFrame → VM → PromQL read-back (live, Docker)
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Single-container Docker walkthrough. Spins up `victoriametrics/victoria-metrics:latest` locally, scaffolds a Dagster project that generates IoT sensor time-series, ingests it into VictoriaMetrics via the Prometheus text-format import endpoint, and reads back a PromQL aggregate.

**Live-validated** — `setup_victoriametrics_demo.sh` + `dg launch --assets '*'` materializes 10,000 synthetic sensor samples into VM against a real container. Verified at component release time; the 3 VM components carry `validation: live` on their manifest entries.

## Components used

| Component | Role |
|---|---|
| [`synthetic_data_generator`](https://dagster-component-ui.vercel.app/c/synthetic_data_generator) | Generates 10,000 IoT sensor readings (`schema_type: sensors`) — timestamp + value + 5 label columns (sensor_id, sensor_type, location, unit, status) |
| [`victoriametrics_resource`](https://dagster-component-ui.vercel.app/c/victoriametrics_resource) | Connection resource (base URL + auth helpers); included for catalog discoverability |
| [`dataframe_to_victoriametrics`](https://dagster-component-ui.vercel.app/c/dataframe_to_victoriametrics) | Bulk-ingest via `/api/v1/import/prometheus` — Prometheus text format with the 5 label columns as Prometheus labels |
| [`victoriametrics_query_asset`](https://dagster-component-ui.vercel.app/c/victoriametrics_query_asset) | PromQL `avg(sensor_reading) by (sensor_type)` query over the last 24h — read-back into a DataFrame |

## Architecture

```
sensor_readings (synthetic_data_generator)
      │
      ▼
vm_sensor_ingest (dataframe_to_victoriametrics)
      │
      ▼ (deps:)
vm_avg_by_sensor_type (victoriametrics_query_asset → PromQL aggregate)
```

## Run

```bash
bash setup_victoriametrics_demo.sh victoriametrics-demo

cd victoriametrics-demo
export VM_BASE_URL=http://localhost:18428

uv run dg launch --assets '*'
# OR uv run dg dev → http://localhost:3000
```

## Verify

Use a **range query** for verification — an instant query (`/api/v1/query?query=sensor_reading`) at "now" will likely miss because the synthetic generator distributes 10,000 samples across the prior 24h. None of those land exactly at "now" within VM's 5-minute staleness window.

```bash
NOW=$(date +%s); BACK=$((NOW - 86400))
curl "http://localhost:18428/api/v1/query_range?query=count(sensor_reading)&start=${BACK}&end=${NOW}&step=600"
```

Expected: a JSON matrix with `values` arrays of `[timestamp, count]` pairs — counts ramp up to a few hundred as the synthetic timestamps cluster in time.

## What the script does

1. **Starts VictoriaMetrics single-node in Docker** (`victoriametrics/victoria-metrics:latest`) on host port 18428 with `-retentionPeriod=1d`. Plenty of room for the 24h synthetic samples.
2. **Scaffolds the Dagster project** with `uvx create-dagster project`.
3. **Installs the 4 components** via the community CLI (`--refresh` on first call).
4. **Overwrites the CLI-installed example defs.yamls** with demo-specific configuration — `sensors` schema for the generator, `sensor_reading` metric name on the VM sink, `avg(sensor_reading) by (sensor_type)` for the read-back, and `deps: [vm_sensor_ingest]` to ensure the read-back asset materializes after the ingest.

## Teardown

```bash
docker rm -f victoriametrics-demo-server
rm -rf /tmp/victoriametrics-demo
```

## Common issues

- **Instant query returns empty result** — use a `query_range` over a window that covers your synthetic data. Synthetic sensors span the past 24h; an instant query at "now" only matches samples within VM's 5-min staleness window. The component handles this correctly (the query asset uses `query_range: true` by default), but ad-hoc curl checks should follow suit.
- **VM warns "timestamp outside retention"** — your synthetic samples are older than `-retentionPeriod`. The demo's `1d` retention covers `synthetic_data_generator`'s 24h window. If you customize the generator's `target_date` to historical, increase VM retention to match.
- **Component not found** — your CLI cache is stale. The script uses `--refresh` on the first `add` to force a fresh manifest fetch.

## See also

- [`vendors/victoriametrics.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/victoriametrics.md) — full VM vendor page (roadmap includes `victoriametrics_snapshot_job` + `victoriametrics_cardinality_sensor`)
- [VictoriaMetrics docs](https://docs.victoriametrics.com/)
- [Prometheus exposition format docs](https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format)
