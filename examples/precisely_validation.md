# Precisely Connect ETL — external asset + status sensor

Bridges a [Precisely Connect ETL](https://www.precisely.com/product/precisely-connect/connect) (formerly Syncsort DMX / DMExpress) job with Dagster as a **declare-only external asset** that gets materialization events from a paired **status-polling sensor**. Precisely owns the schedule + execution; Dagster owns the catalog + observability.

## The two-component pattern

Precisely Connect ETL publishes exactly one REST surface — `GET /projects/{jobRunId}/status` — and it returns a plain-text status string. There is **no publicly documented submit / trigger endpoint** and **no list-runs endpoint**. So Dagster can't "run" a Precisely job. What it CAN do is treat the job as a real first-class asset, and surface every observed Precisely run as a materialization event:

| Component | Role |
|---|---|
| [`external_precisely_job`](https://dagster-component-ui.vercel.app/c/external_precisely_job) | Declare-only `AssetSpec`. Makes the Precisely job visible in the asset graph (downstream assets can `deps: [...]`, lineage shows up, materialization history is a real catalog feature). No execution function — `dagster.observability_type=external`. |
| [`precisely_job_sensor`](https://dagster-component-ui.vercel.app/c/precisely_job_sensor) | Polls Precisely's documented Job Status endpoint. On terminal `COMPLETED` / `COMPLETED_WITH_WARNINGS`, emits `AssetMaterialization(asset_key=<same as external asset>)` AND fires a `RunRequest` against any downstream Dagster job you name. |

The `asset_key` field on both components must match — that's the glue.

```
                            ┌─────────────────────────────────┐
   Precisely runs           │  Dagster catalog (declare-only) │
   on its own schedule      │                                 │
   ────────────────►        │  precisely/etl/load_customers   │
                            │  (external_precisely_job)       │
                            │           ▲                     │
                            │           │ AssetMaterialization│
                            │           │ on terminal SUCCESS │
                            │  precisely_job_sensor ──RunRequest──►  downstream Dagster @job
                            └─────────────────────────────────┘
```

## API verification

The sensor polls the **single verified endpoint** from Precisely's public REST docs:

| Aspect | Sensor | [Precisely docs](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status) |
|---|---|---|
| HTTP method + path | `GET {host}/projects/{jobRunId}/status` | `GET projects/<jobRunId>/status` |
| Response format | plain text (`resp.text.strip().upper()`) | plain text |
| Status enum (success) | `COMPLETED`, `COMPLETED_WITH_WARNINGS` | same |
| Status enum (failure) | `COMPLETED_WITH_ERRORS`, `CANCELLED`, `ERRORED`, `LOST_CONTACT` | same |
| Status enum (in-progress) | `WAITING`, `RUNNING` | same |

## Validation status — what we CAN claim vs. theoretical

| Aspect | Validation level | How |
|---|---|---|
| Components compile + `dg check defs` passes | ✅ **Verified** | `setup_precisely_validation_demo.sh` + `dg check defs` on a fresh project |
| Polled URL + method + response parsing matches Precisely's public REST docs | ✅ **Verified** | Side-by-side diff against the [Job Status spec](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status) — see the API verification table above |
| External asset appears in catalog with sensor wired | ✅ **Verified** | `dg dev` loads both components cleanly; asset graph shows `precisely/etl/load_customers` |
| Sensor fires a `RunRequest` + emits `AssetMaterialization` against a real Precisely Connect ETL cluster | ⚠ **NOT verified** — we don't have a Precisely cluster to validate against |
| Customer-side end-to-end (Precisely run → sensor → materialization on external asset + RunRequest) | ⚠ **NOT verified** — depends on the live-validation gap above |

Shipped at validation level `code` because of the bottom two rows. When a customer with a real Precisely Connect ETL install runs this against a live job-run-id and we observe a clean fire, we'll promote to `live`.

## Demo

```bash
bash setup_precisely_validation_demo.sh precisely-demo
cd precisely-demo
export PRECISELY_HOST=https://your-precisely-host    # placeholder OK for compile-check
export PRECISELY_API_TOKEN=placeholder
uv run dg dev
```

The scaffolded project:

1. Installs `external_precisely_job` and `precisely_job_sensor`
2. Writes a `defs.yaml` for each — both pinned to the same `asset_key`
3. Registers a no-op downstream Dagster job (`precisely_downstream_job`) as the sensor's `RunRequest` target
4. Starts the sensor in `stopped` state (you flip it on in the UI once you have a real `job_run_id`)

`dg dev` loads cleanly and shows: the external asset `precisely/etl/load_customers` in the catalog, the sensor in the sensors tab, and the downstream job in the jobs tab. Once you have a real run-id, edit the two `defs.yaml` files, set `default_status: running` on the sensor, and `dg dev` will poll and fire on terminal SUCCESS — materializing the external asset and firing the downstream job in one tick.

## Customer-facing shape

```yaml
# defs/external_precisely_job/defs.yaml
type: dagster_community_components.ExternalPreciselyJobAsset
attributes:
  asset_key: precisely/etl/load_customers
  job_id: "abc-123-xyz"                       # stable Precisely job id
  host: https://precisely.mycompany.com
  group_name: precisely
  description: |
    Precisely Connect ETL job that lands customer records.
    Materialized externally; Dagster observes via precisely_job_sensor.
```

```yaml
# defs/precisely_job_sensor/defs.yaml
type: dagster_community_components.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_run_id: "abc-123-def-456"               # the Precisely run-id you want to watch
  host_env_var: PRECISELY_HOST
  api_token_env_var: PRECISELY_API_TOKEN
  job_name: downstream_processing_job
  asset_key: precisely/etl/load_customers     # SAME as external asset above
  minimum_interval_seconds: 60
  default_status: running
```

## See also

- [`external_precisely_job` component README](https://dagster-component-ui.vercel.app/c/external_precisely_job)
- [`precisely_job_sensor` component README](https://dagster-component-ui.vercel.app/c/precisely_job_sensor)
- [Precisely Connect ETL — Job Status REST endpoint docs](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status)
