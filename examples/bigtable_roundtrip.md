# Bigtable Round-trip — write + read in one Dagster pipeline
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end against real APIs** (servicepulse-490502, demo-instance, demo-table). DataFrame → Bigtable → DataFrame, with column-family routing and JSON encoding for nested values.

```
sensor_readings        ← synthetic_data_generator (sensors schema)
       │
       └── sensors_written        ← bigtable_writer_asset
                                    (writes to demo-instance / demo-table)
              │
              └── sensors_readback  ← bigtable_reader_asset
                                      (scans SENS prefix, decodes utf-8)
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_data_generator` | Synthetic upstream. `schema_type: sensors` produces `(sensor_id, timestamp, sensor_type, location, value, unit, status)` — fits Bigtable's wide-column model naturally. |
| `bigtable_writer_asset` | One DataFrame row → one Bigtable row. `row_key_column` + per-column family/qualifier `column_map` + `json_columns` for dict/list values. |
| `bigtable_reader_asset` | Prefix or range scan → DataFrame with `_row_key` + one column per `<family>:<qualifier>` (latest cell). |

## Live run output

- **Writer**: 5 rows written to `servicepulse-490502/demo-instance/demo-table` in 2.27s. Column family/qualifier mapping:
  ```
  meta:ts        → last_seen (timestamp)
  meta:labels    → labels (JSON-encoded dict)
  metrics:temp_c → temperature
  metrics:humidity_pct → humidity
  ```
- **Reader**: 5 rows scanned via `device#` prefix in 1.47s. Sample output:
  ```
  device#001  meta:ts=2026-05-11T08:00:00Z, meta:labels={"site":"hq",...}, metrics:temp_c=22.4, metrics:humidity_pct=41
  device#002  meta:ts=2026-05-11T08:01:00Z, ...
  ```

## Performance

- Writer batches 500 rows per `mutate_rows()` call by default. Tune via `batch_size`.
- Reader is a single streaming scan with optional `limit`. No batching needed.

## Cost

- **Instance: ~$0.65/hr per node (SSD, 1 node minimum).** Storage extra.
- Reads/writes themselves are billed in mutation/read units but pennies at this scale.
- **Tear down when done** — instances bill while they exist:
  ```bash
  gcloud bigtable instances delete demo-instance --project=$GCP_PROJECT_ID --quiet
  ```

## Required setup (one-time)

```bash
# 1. Enable APIs
# https://console.cloud.google.com/apis/library/bigtable.googleapis.com
# https://console.cloud.google.com/apis/library/bigtableadmin.googleapis.com

# 2. Create instance
gcloud bigtable instances create demo-instance \
  --display-name="Dagster demo" \
  --cluster-config=id=demo-cluster,zone=us-central1-a,nodes=1,type=SSD

# 3. Create table + column families (via Python)
python <<'PY'
from google.cloud import bigtable
from google.cloud.bigtable import column_family
c = bigtable.Client(project='YOUR-PROJECT', admin=True)
t = c.instance('demo-instance').table('demo-table')
t.create(column_families={'meta': column_family.MaxVersionsGCRule(1),
                          'metrics': column_family.MaxVersionsGCRule(1)})
PY

# 4. IAM grant
gcloud bigtable instances add-iam-policy-binding demo-instance \
  --member="serviceAccount:$SA_EMAIL" --role="roles/bigtable.user"
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_bigtable_roundtrip_demo.sh | bash
cd bigtable-roundtrip-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
