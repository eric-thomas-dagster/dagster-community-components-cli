# Precisely Connect ETL components — validation against public docs

The registry has two Precisely components:

- [`precisely_run_asset`](https://dagster-component-ui.vercel.app/c/precisely_run_asset)
  — triggers a Precisely Connect ETL job and waits for it to complete.
- [`precisely_job_sensor`](https://dagster-component-ui.vercel.app/c/precisely_job_sensor)
  — fires a Dagster RunRequest when a Precisely job reaches a terminal SUCCESS state.

Without a Precisely account we can't run them end-to-end, but we can
verify their REST API surface against Precisely's public documentation.

## What's verified ✅

The **Job Status** endpoint matches Precisely's published Connect ETL
REST API spec:

| Aspect | Component (after fix) | Precisely public docs |
|---|---|---|
| HTTP method + path | `GET {host}/projects/{jobRunId}/status` | `GET projects/<jobRunId>/status` |
| Response format | plain text status string (`resp.text.strip().upper()`) | plain text status string |
| Status enum (success) | `COMPLETED`, `COMPLETED_WITH_WARNINGS` | same |
| Status enum (failure) | `COMPLETED_WITH_ERRORS`, `CANCELLED`, `ERRORED`, `LOST_CONTACT` | same |
| Status enum (in-progress) | `WAITING`, `RUNNING` | same |

Source: [Job Status — Connect_ETL — Latest](https://help.precisely.com/r/Connect-ETL/pub/Latest/en-US/Connect-ETL-Rest-API-Reference/Job-Status)

## What's NOT verified ⚠️

The **submit / trigger** endpoint and the **list-runs** endpoint are not
in Precisely's public REST documentation (the relevant pages require
customer-portal authentication). The components fall back to best-guess
RESTful shapes:

| Endpoint | Best-guess default | Override via |
|---|---|---|
| Submit a job | `POST {host}/projects/{job_id}/run` with JSON `{"parameters": {...}}` | `submit_path_template` field on `PreciselyResource` and [`precisely_run_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/precisely_run_asset) |
| List recent runs of a job (sensor only) | `GET {host}/api/v1/jobs/{job_id}/runs?limit=1&sort=-startTime` | `list_runs_path_template` field on [`precisely_job_sensor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/precisely_job_sensor) |

If these defaults don't match your install's API, override the field
in your `defs.yaml` — the component logic will still poll the correct
Job Status endpoint regardless.

## Bugs found and fixed

The pre-fix component used a **completely fictional** API surface:

- ❌ Status path: `GET /api/v1/jobs/{job_id}/runs/{run_id}` returning JSON
- ❌ Status enum: looked for `SUCCESS` / `SUCCEEDED` / `ABORTED` (none of which Precisely returns)
- ❌ Treated response as JSON; would crash on plain-text response

After the fix, the verified Job Status path is wired correctly. The
unverified submit / list-runs paths are field-overridable so customers
can plug in their actual paths without modifying the component.

## Sensor: two modes

The fix added a second sensor mode that uses **only** the verified API:

```yaml
# Mode A: watch a known job-run ID. Uses verified Job Status endpoint only.
type: dagster_component_templates.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_run_id: "abc-123-def-456"      # the run-id you want to watch
  host_env_var: PRECISELY_HOST
  api_token_env_var: PRECISELY_API_TOKEN
  job_name: downstream_processing_job
```

```yaml
# Mode B: watch latest run of a job (uses unverified list-runs path).
type: dagster_component_templates.PreciselyJobSensorComponent
attributes:
  sensor_name: precisely_etl_done
  job_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  list_runs_path_template: "/api/v1/jobs/{job_id}/runs"  # validate vs your install
  ...
```

Mode A is the safe default for customers who can pre-determine the
run-id (e.g., from [`precisely_run_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/precisely_run_asset)'s materialization metadata).
Mode B requires validation against the customer's install.

## What we still don't have

- **End-to-end test against a real Precisely Connect ETL instance.**
  Both components are at validation level `code` (parses + matches
  documented API spec). Promoting to `live` requires a customer
  validation run.
- **Customer-portal-only documentation.** The submit and list-runs
  endpoints might be specified in Precisely's customer-restricted
  docs. If a customer can confirm those paths, the field overrides
  let them apply the verified shape without code changes.
