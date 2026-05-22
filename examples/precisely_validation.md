# Precisely Connect ETL — sensor-only

Bridges a [Precisely Connect ETL](https://www.precisely.com/product/precisely-connect/connect) (formerly Syncsort DMX / DMExpress) job with Dagster. **Sensor-only**: Precisely owns the schedule + execution; Dagster watches for a job-run reaching terminal SUCCESS and fires a `RunRequest` for downstream work.

## Why sensor-only?

Precisely Connect ETL publishes exactly one REST endpoint — `GET /projects/{jobRunId}/status` — and it returns a plain-text status string. There is **no publicly documented submit / trigger endpoint** and **no list-runs endpoint**. Earlier versions of this integration tried to POST a "best-guess" `/projects/{jobId}/run`, which 404s in every real customer install.

So the integration shape is:

- **Precisely** runs your ETL on its own schedule, on its own infrastructure.
- **Dagster** polls `GET /projects/{jobRunId}/status` on a minimum-interval cadence.
- When the run reaches `COMPLETED` or `COMPLETED_WITH_WARNINGS`, the sensor emits a `RunRequest` for whatever downstream Dagster job you've named.

That's the whole pattern. No asset, no execution, no fake-submit pretense.

## Components

Only **one component** ships:

| Component | Purpose |
|---|---|
| [`precisely_job_sensor`](https://dagster-component-ui.vercel.app/c/precisely_job_sensor) | Watch a Precisely Connect ETL job-run via the documented Job Status endpoint; fire `RunRequest` on terminal SUCCESS |

## API verification

The sensor polls the **single verified endpoint** from Precisely's public REST docs:

| Aspect | Sensor | [Precisely docs](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status) |
|---|---|---|
| HTTP method + path | `GET {host}/projects/{jobRunId}/status` | `GET projects/<jobRunId>/status` |
| Response format | plain text (`resp.text.strip().upper()`) | plain text |
| Status enum (success) | `COMPLETED`, `COMPLETED_WITH_WARNINGS` | same |
| Status enum (failure) | `COMPLETED_WITH_ERRORS`, `CANCELLED`, `ERRORED`, `LOST_CONTACT` | same |
| Status enum (in-progress) | `WAITING`, `RUNNING` | same |

## Demo

```bash
bash setup_precisely_validation_demo.sh precisely-demo
cd precisely-demo
export PRECISELY_HOST=https://your-precisely-host    # placeholder OK for compile-check
export PRECISELY_API_TOKEN=placeholder
uv run dg dev
```

The scaffolded project:

1. Installs `precisely_job_sensor`
2. Writes a `defs.yaml` pointing at a placeholder `job_run_id`
3. Registers a no-op downstream Dagster job (`precisely_downstream_job`)
4. Starts the sensor in `stopped` state (you flip it on in the UI once you have a real `job_run_id`)

`dg dev` loads cleanly and shows the sensor + job in the UI. No real Precisely instance needed for the compile-check; once you've got a real run-id, edit the `defs.yaml`, set `default_status: running`, and `dg dev` will poll and fire on terminal SUCCESS.

## Customer-facing shape

```yaml
type: dagster_community_components.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_run_id: "abc-123-def-456"          # the Precisely run-id you want to watch
  host_env_var: PRECISELY_HOST
  api_token_env_var: PRECISELY_API_TOKEN
  job_name: downstream_processing_job
  minimum_interval_seconds: 60
  default_status: running
```

## See also

- [`precisely_job_sensor` component README](https://dagster-component-ui.vercel.app/c/precisely_job_sensor)
- [Precisely Connect ETL — Job Status REST endpoint docs](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status)
