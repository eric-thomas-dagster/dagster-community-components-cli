# Temporal + Dagster — Full-Trio End-to-End Demo
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

**Components:**
- `TemporalWorkflowTriggerComponent` (`assets/infrastructure/temporal_workflow_trigger`)
- `ExternalTemporalWorkflowAsset` (`external_assets/external_temporal_workflow`)
- `TemporalWorkflowSensorComponent` (`sensors/temporal_workflow_sensor`)

**Script:** [`setup_temporal_workflow_demo.sh`](./setup_temporal_workflow_demo.sh)
**Cost:** $0 (local Temporal dev server + free public SWAPI API)
**Duration:** ~30 seconds from cold to green
**Validated:** 2026-07-02 (RUN_SUCCESS end-to-end; trigger + sensor both observed)

## What it demonstrates

A complete Dagster ↔ Temporal integration in one project:

```
┌──────────────────┐         gRPC          ┌────────────────────┐
│ Dagster asset    │ ─── start_workflow ─▶ │ Temporal frontend  │
│ planet_summary   │                       │ (localhost:7233)   │
│ (trigger)        │ ◀── wait_for_result ─ └────────────────────┘
└──────────────────┘                                 │
        │ materializes with result                   │ dispatches
        ▼                                            ▼
   Dagster catalog                          ┌────────────────────┐
                                            │ Python worker      │
                                            │ (demo-tq)          │
                                            │ PlanetSummaryWorkflow ─┐
                                            └────────────────────┘   │
                                                                     │ activity
                                                                     ▼
                                                              https://swapi.dev
                                                              (real public API)

┌──────────────────┐   Visibility.list_workflows   ┌────────────────────┐
│ Dagster sensor   │ ─────── poll (30s) ─────────▶ │ Temporal frontend  │
│ (observation)    │                               └────────────────────┘
└──────────────────┘
        │ emits AssetObservation on Completed
        ▼
   external asset temporal/demo/planet_summary_external
```

## Why this trio

Temporal is itself an orchestrator built for durable long-running work. The 80% integration pattern is **Dagster observes, Temporal executes**:

- **`external_temporal_workflow`** — puts Temporal work in the Dagster catalog with lineage, so downstream Dagster assets can `deps: [temporal/etl/nightly_agg]` against it.
- **`temporal_workflow_sensor`** — polls Temporal Visibility, emits `AssetMaterialization`/`AssetObservation` on terminal success, fires downstream Dagster jobs.
- **`temporal_workflow_trigger`** — narrower use case. Dagster's schedule is the source of truth, Dagster fires the workflow. Great for partition-keyed durable work; less appropriate when Temporal owns the schedule.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `temporal` CLI — `brew install temporal` (mac) or the [official install](https://docs.temporal.io/cli)
- Internet access to `swapi.dev`

That's it. No account, no API key, no paid Temporal Cloud tier needed for the local demo.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_temporal_workflow_demo.sh -o setup_temporal_workflow_demo.sh
chmod +x setup_temporal_workflow_demo.sh
./setup_temporal_workflow_demo.sh                        # → temporal_demo/
./setup_temporal_workflow_demo.sh my_temporal_proj       # custom name
```

## What the script does

1. **Preflight** — verifies `uv` and `temporal` are installed; refuses to start if port 7233 is already busy.
2. **Starts `temporal server start-dev`** — headless, SQLite backend, on `localhost:7233` (frontend) and `localhost:8233` (web UI).
3. **Scaffolds a Dagster project** via `uvx create-dagster project`.
4. **Installs deps** — `dagster-community-components`, `temporalio>=1.7.0`, `httpx>=0.27`.
5. **Writes `temporal_worker/worker.py`** — a real Temporal worker that registers a `PlanetSummaryWorkflow` and a `fetch_planet` activity that hits `https://swapi.dev`.
6. **Starts the worker** as a background process polling task queue `demo-tq`.
7. **Writes three defs.yaml files** — one for each Temporal component.
8. **Materializes** `temporal/demo/planet_summary` via `dagster asset materialize` — Dagster starts a real Temporal workflow, waits for the result, and stores it in asset metadata.
9. **Prints next steps** — the services stay running so you can open the Temporal UI, run `dg dev`, or watch the sensor tick.

## The three defs

Trigger — Dagster owns the schedule, fires the workflow, waits, captures result:

```yaml
type: dagster_community_components.TemporalWorkflowTriggerComponent
attributes:
  asset_key: temporal/demo/planet_summary
  workflow_type: PlanetSummaryWorkflow
  task_queue: demo-tq
  workflow_id: "planet-summary-{run_id}"
  workflow_arg: 1        # Tatooine
  target_host: localhost:7233
  namespace: default
  wait_for_result: true
  result_wait_timeout_seconds: 60
```

External asset — declare Temporal work in the Dagster catalog (Temporal owns the schedule):

```yaml
type: dagster_community_components.ExternalTemporalWorkflowAsset
attributes:
  asset_key: temporal/demo/planet_summary_external
  workflow_type: PlanetSummaryWorkflow
  namespace: default
  task_queue: demo-tq
  temporal_ui_url: http://localhost:8233
```

Sensor — observe terminal state, emit AssetObservation, fire downstream job:

```yaml
type: dagster_community_components.TemporalWorkflowSensorComponent
attributes:
  sensor_name: temporal_planet_summary_done
  list_filter: "WorkflowType='PlanetSummaryWorkflow' AND ExecutionStatus='Completed'"
  job_name: __ASSET_JOB
  asset_key: temporal/demo/planet_summary_external
  asset_event_type: observation
```

## Validated run (2026-07-02)

```
▸ Starting Temporal dev server on :7233 (UI on :8233)…
✓ Temporal server up (pid 10700)
▸ Scaffolding Dagster project…
▸ Installing dependencies (dagster-community-components + temporalio + httpx)…
✓ Dependencies installed
▸ Starting Temporal worker (task queue 'demo-tq')…
✓ Worker up (pid 10730)
▸ Materializing temporal/demo/planet_summary (starts + waits for workflow)…
[temporal] starting workflow_type='PlanetSummaryWorkflow' workflow_id='planet-summary-...' task_queue='demo-tq' ...
[temporal] started run_id='019f2544-68ef-7cb9-b90b-27096ed36968'
[temporal] workflow completed successfully
temporal__demo__planet_summary — STEP_SUCCESS — 634ms
RUN_SUCCESS
✓ Workflow completed via Dagster asset
```

`temporal workflow list` confirms:

```
Status                        WorkflowId                               Type             StartTime
Completed  planet-summary-91aa60ff-a25e-43e0-b7e1-d836630caeaa  PlanetSummaryWorkflow  15 seconds ago
```

Asset metadata captured by Dagster:

```
temporal_workflow_id:   planet-summary-91aa60ff-a25e-43e0-b7e1-d836630caeaa
temporal_run_id:        019f2544-68ef-7cb9-b90b-27096ed36968
temporal_workflow_type: PlanetSummaryWorkflow
temporal_task_queue:    demo-tq
temporal_namespace:     default
temporal_target_host:   localhost:7233
temporal_result:        {
                          "planet_id": 1,
                          "name": "Tatooine",
                          "climate": "arid",
                          "terrain": "desert",
                          "population": "200000",
                          "diameter_km": "10465"
                        }
```

Sensor tick (validated separately against the same completed workflow):

```
RunRequest(run_key='planet-summary-...:019f2544-...', tags={
  'temporal/workflow_id': 'planet-summary-...',
  'temporal/run_id':      '019f2544-...',
  'temporal/status':      'COMPLETED',
  'temporal/namespace':   'default',
})
AssetObservation(asset_key=temporal/demo/planet_summary_external,
                 description='Temporal workflow ... → COMPLETED',
                 metadata={temporal_workflow_id, temporal_run_id, temporal_status,
                           temporal_namespace, temporal_close_time})
```

## Temporal Cloud

Same components, same code path. Point at Cloud in the YAML:

```yaml
target_host: myns.abcde.tmprl.cloud:7233
namespace:   myns.abcde                    # <namespace>.<account_id>
# API-key auth:
api_key_env_var: TEMPORAL_CLOUD_API_KEY
# OR mTLS:
tls_cert_env_var: TEMPORAL_TLS_CERT        # PEM-encoded client cert
tls_key_env_var:  TEMPORAL_TLS_KEY         # PEM-encoded private key
```

Notes:
- API-key auth requires `temporalio>=1.8.0`.
- Bump the sensor's `minimum_interval_seconds` to 60+ on Cloud — Visibility API rate limits are stricter than local dev.
- Cloud namespace is `<name>.<account_id>` — easy footgun.

## Extension ideas

- **Partitioned pipelines.** Set `workflow_id: "aggregation-{partition_key}"` on the trigger. Each partition run kicks a separate Temporal workflow, all trackable in the Dagster asset catalog and the Temporal UI.
- **Long-running signals.** Use `wait_for_result: false` on the trigger + the sensor with `asset_event_type: observation` — fire-and-forget from Dagster, observe completion asynchronously.
- **Chain to Dagster assets.** Add `deps: ["temporal/etl/nightly_agg"]` on a downstream Dagster asset — it'll depend on the Temporal completion via the sensor's materializations.
- **Cross-cluster.** Point trigger at one namespace + sensor at a different one to bridge two Temporal deployments through Dagster.

## Requirements

```
temporalio>=1.7.0        # >=1.8.0 for Temporal Cloud API-key auth
httpx>=0.27              # only for the demo activity — not required by the components
```

## See also

<!-- TODO: link related walkthroughs -->
