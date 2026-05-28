# InfluxDB 2.x — DataFrame → Flux query (live, Docker)

Single-container Docker walkthrough. Spins up `influxdb:2-alpine` with a pre-initialized org + bucket + admin token, scaffolds a Dagster project that generates IoT sensor data, and bulk-writes 10,000 points into InfluxDB via the official `influxdb-client` Python SDK.

**Live-validated** — `setup_influxdb_demo.sh` + `dg launch --assets '*'` writes 10,000 sensor samples into InfluxDB. Verified via Flux `count()` query — distinct series materialize correctly across all 5 tag columns.

## Components exercised

| Component | Role |
|---|---|
| [`synthetic_data_generator`](https://dagster-component-ui.vercel.app/c/synthetic_data_generator) | 10,000 IoT sensor readings (`schema_type: sensors`) — timestamp + value + 5 tag-able columns |
| [`influxdb_resource`](https://dagster-component-ui.vercel.app/c/influxdb_resource) | Connection resource (URL + token + org + bucket via official `influxdb-client` SDK) |
| [`dataframe_to_influxdb`](https://dagster-component-ui.vercel.app/c/dataframe_to_influxdb) | Bulk-write via `write_api` line-protocol path. Numeric → fields, others → tags (auto-classified). |

## Asset graph

```
sensor_readings (synthetic_data_generator)
      │
      ▼
influxdb_sensor_write (dataframe_to_influxdb)
```

## Run it

```bash
bash setup_influxdb_demo.sh influxdb-demo

cd influxdb-demo
export INFLUXDB_URL=http://localhost:18086
export INFLUXDB_TOKEN='dagster-demo-token-do-not-use-in-prod'
export INFLUXDB_ORG='dagster-org'

uv run dg launch --assets '*'
# OR uv run dg dev → http://localhost:3000
```

## Verify (Flux)

```bash
curl -X POST 'http://localhost:18086/api/v2/query?org=dagster-org' \
  -H 'Authorization: Token dagster-demo-token-do-not-use-in-prod' \
  -H 'Content-Type: application/vnd.flux' \
  --data 'from(bucket:"metrics") |> range(start: -25h) |> filter(fn:(r) => r._measurement == "sensor_reading") |> count()'
```

Expected: CSV rows broken down by (sensor_id, sensor_type, location, status, unit) tag combinations. Each row's `_value` is the count of samples in that series.

## InfluxDB UI

The Docker container also exposes the InfluxDB web UI:

- **URL**: http://localhost:18086
- **Login**: `admin` / `influxdb-admin`
- **Org**: `dagster-org`
- **Bucket**: `metrics`

Open the UI to see the data interactively, build dashboards, or experiment with Flux queries against the demo data.

## Tags vs fields — what the auto-classifier did

`dataframe_to_influxdb` auto-classified the synthetic `sensors` DataFrame columns:

| Column | Dtype | Classified as |
|---|---|---|
| `timestamp` | datetime | Index (per `timestamp_column:`) |
| `sensor_id` | string | Tag |
| `sensor_type` | string | Tag |
| `location` | string | Tag |
| `value` | float | Field |
| `unit` | string | Tag |
| `status` | string | Tag |

The demo's `defs.yaml` makes this explicit via `tag_columns:` + `field_columns:` for repeatability, but you can drop both for auto-classification (numeric → field; others → tag).

## Common issues

- **`429 Too Many Requests`** — bursting too many writes. Component batches via `batch_size: 5000` by default; tune lower if rate-limited.
- **`401 Unauthorized`** — token doesn't have write permission on the bucket. The demo's pre-seeded admin token has full access.
- **Tag cardinality explosion** — putting high-cardinality columns (user_id, request_id) as TAGS hurts InfluxDB performance. Use them as fields. The auto-classifier helps (numeric → field) but watch string columns.

## Cleanup

```bash
docker rm -f influxdb-demo-server
rm -rf /tmp/influxdb-demo
```

## See also

- [`vendors/influxdb.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/influxdb.md) — full InfluxDB vendor page (covers AWS Timestream for InfluxDB too)
- [InfluxDB 2.x docs](https://docs.influxdata.com/influxdb/v2/) / [3.x docs](https://docs.influxdata.com/influxdb3/)
- [`influxdb-client` Python SDK](https://github.com/influxdata/influxdb-client-python)
