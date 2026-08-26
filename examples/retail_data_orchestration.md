# Retail Data Orchestration

> **What this is.** A three-scenario Dagster demo shaped around the enterprise data-orchestration patterns most retail data teams need to prove out before adopting a replacement orchestrator. Everything scaffolds end-to-end from one script; the walkthrough below maps every requirement to specific components + config so it's clear which piece of the demo satisfies which requirement.

**Three ways to build this:**

- **Fast local scaffold (~3 min)** — run the [setup script](setup_retail_data_orchestration_demo.sh) to get the laptop-runnable demo. No credentials required. Best for inspecting the output shape.
- **Local from scratch (~30 min, teach-yourself)** — follow the [local hands-on tutorial](retail_data_orchestration_from_scratch.md). Uses stand-ins (MinIO / DuckDB / dbt Core); type every command yourself. No credentials required.
- **Real POC from scratch (~1 hr, hands-on with real credentials)** — follow the [real POC hands-on tutorial](retail_data_orchestration_real_from_scratch.md). Builds directly against real Snowflake / dbt Cloud / HVR / Power BI. Uses `snowflake_workspace` and `hvr_hub_workspace` as the primary Snowflake + HVR surfaces (one YAML block per system, not one per object).

**Fast setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_retail_data_orchestration_demo.sh -o setup.sh
chmod +x setup.sh
./setup.sh
```

**What you get:**

- A canonical `create-dagster` project with community components installed as **files inside the project** (`src/retail_data_orchestration/components/<id>/component.py`) — no library import to inspect through. Every component's source is right there, copy-editable.
- Three scenarios wired as separate defs directories:
  - **Scenario 1** — Python extract → SnowPipe load → dbt build → mart (triggered by load-completion evidence, not a timer)
  - **Scenario 2** — External replication API refresh → dbt build (triggered by verified completion)
  - **Scenario 3** — State-awareness pattern: skip a task when its data is already fresh
- A `dbt_project/` (dbt Core against DuckDB, laptop-local) stands in for dbt Cloud in the demo. The walkthrough shows the single-line YAML swap for real dbt Cloud.
- A `docker-compose.yml` with MinIO (S3-compatible object store) so the extract → object-store → load chain runs on a laptop with zero credentials.
- Sample data + a Python extract script wired through `shell_command_asset` — reusing the existing extract, unchanged, not rewriting it into ops.

---

## Architecture (local demo)

```
   [ Python extract (shell_command_asset) ]
                    │  writes N files
                    ▼
              [ MinIO (S3) ]
                    │  file arrival
                    ▼
    [ filesystem_monitor sensor ]        ◄── stand-in for snowflake_snowpipe_load_sensor
                    │  fires on load completion
                    ▼
      [ dbt build (shell_command_asset) ]  ◄── stand-in for dbt_cloud_trigger_job + dbt_cloud_resource
                    │
                    ▼
         [ DuckDB mart tables ]           ◄── stand-in for Snowflake mart
                    │
                    ▼
      [ FreshnessPolicy + AutomationCondition ]  (scenario 3)
                    │
                    ▼
       [ Power BI semantic model refresh ]        (stretch — commented in defs.yaml)
```

**For the real POC**, four YAML swaps convert every stand-in to the real component. Each is called out in the walkthrough section below and copy-pasteable from [`POC_REAL_MODE.md`](retail_data_orchestration_real_mode.md) that the setup script emits.

---

## Scenario 1 — API extract → object store → load → dbt

**Story.** A Python extract runs on schedule and writes an unpredictable number of files to an object-store prefix. A load listener detects that *the load resulting from that extract has completed* — not "elapsed 2 minutes" — and triggers a downstream dbt build. The chain is one pipeline with one status; the load step is a visible node with its own state.

**Defs directory:** `src/retail_data_orchestration/defs/scenario1_api_load_dbt/`

**Component wiring:**

```yaml
# extract.yaml — runs the existing Python extract unchanged (G7 - reuse pipelines)
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: raw_api_files
  command: "python /workspace/extract_scripts/api_extract.py --out /workspace/data/incoming"
  group_name: scenario1_api_load_dbt
  kinds: [python, api]
  # partition-per-day if you want a daily backfill grain:
  # partition_type: daily
  # partition_start: "2026-01-01"
```

```yaml
# load_completion_sensor.yaml — fires when ALL expected files landed. Solves
# the "how do we know the extract is done writing?" question for a multi-file
# extract without changing the extract itself.
type: retail_data_orchestration.components.filesystem_monitor.FilesystemMonitorSensorComponent
attributes:
  sensor_name: api_load_complete_sensor
  directory_path: /workspace/data/incoming
  file_pattern: ".*\\.json\\.gz$"
  job_name: dbt_build_mart_job
  minimum_interval_seconds: 30
  recursive: true
  default_status: running
```

```yaml
# dbt.yaml — dbt build against DuckDB. Emits per-model assets w/ test results
# as first-class pass/fail signals (OBS-05). Swap to dbt_cloud_trigger_job for real
# dbt Cloud (see retail_data_orchestration_real_mode.md).
type: retail_data_orchestration.components.dbt_project.DbtProjectComponent
attributes:
  project_dir: /workspace/dbt_project
  select: "tag:scenario1"
  # Emitted asset per dbt model + one asset check per dbt test.
  # Downstream sensors + FreshnessPolicy hang off these directly.
```

**Real-mode swaps** (each is one YAML file replacement; see [real-mode swap guide](retail_data_orchestration_real_mode.md) or start from scratch against real credentials with the [real POC hands-on tutorial](retail_data_orchestration_real_from_scratch.md)):

| Local demo | Real POC (recommended) | Effect |
|---|---|---|
| `filesystem_monitor` + `shell_command_asset` (dbt) | **`snowflake_workspace`** with `import_snowpipes: true` + `import_tables: true` + `polling_sensor: true` | ONE YAML block replaces both — discovers every Snowpipe + landing/mart table under the schema, and the polling sensor emits load-completion events for TRG-01/OBS-03. |
| `shell_command_asset` extract | `shell_command_asset` unchanged | The Python script writes to real S3 instead of MinIO — env var swap only, no component change (G7). |
| `shell_command_asset` (dbt) | `dbt_cloud_resource` + `dbt_cloud_trigger_job` (+ optional `dbt_cloud_job_sensor`) | Registers the dbt Cloud connection, then wraps `DbtCloudResource.run_job_and_poll` in a Dagster op-job any sensor / schedule / upstream event can target by name. Emits per-model `AssetMaterialization` events for keys in `emit_materializations_for`, wiring the run into the asset graph. `dbt_cloud_job_sensor` is the reverse direction (observes dbt Cloud, triggers Dagster downstream) — optional. **`dbt_run_job` runs dbt Core via subprocess, NOT dbt Cloud — don't confuse them.** |
| Mock replication CLI | **`hvr_hub_workspace`** with `action: refresh` + `polling_sensor: true` | ONE YAML block replaces the shim — POST /refresh + polls completion + emits per-table observation events with integrate-lag metadata. |
| — (add) | `dagster-powerbi` `PowerBIWorkspace` | S1.8 stretch — official Dagster integration, not a community component. Emits report/dataset/semantic-model assets hanging off Snowflake mart. |
| — (add) | `dbt_state_reuse_patch` | Scenario 3 Mechanism A — treats dbt Cloud `no-op` responses as materialization events instead of failures. |

**Piecemeal alternative** — when the workspace defaults don't fit, use `snowflake_snowpipe_load_sensor` / `snowflake_snowpipe` / `external_snowflake_table` individually. See the [real-mode swap guide](retail_data_orchestration_real_mode.md#1a-alt-piecemeal--snowflake_snowpipe_load_sensor-for-finer-control) for the piecemeal path.

---

## Scenario 2 — External replication API refresh → dbt

**Story.** For the subset of tables that get *fully* refreshed on a schedule (via a replication vendor's API — HVR, Fivetran, or similar), the orchestrator calls the vendor API, polls for actual completion (not "API accepted → assume done"), then triggers the dbt build. The replication step is a visible node with its own state, duration, and error surface.

**Defs directory:** `src/retail_data_orchestration/defs/scenario2_replication_refresh/`

**Component wiring:**

```yaml
# refresh.yaml — LOCAL demo uses shell_command_asset to call a mock refresh API.
# Real-mode swap: hvr_hub_workspace with action:refresh + wait_for_completion.
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: replicated_landing_tables
  command: "python /workspace/extract_scripts/mock_replication_refresh.py"
  group_name: scenario2_replication_refresh
  kinds: [replication, hvr]
```

Real-mode replacement:

```yaml
type: retail_data_orchestration.components.hvr_hub_workspace.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"
    hub_name: "{{ env.HVR_HUB_NAME }}"
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
  channel_selector:
    by_pattern: ["fuel_price*"]
  action: refresh                    # POST /channels/{c}/refresh + poll
  wait_for_completion: true
  poll_interval_seconds: 30
  timeout_seconds: 1800
  polling_sensor: true               # AssetObservation per (channel × target × table)
  observation_interval_seconds: 300
  freshness_lag_threshold_seconds: 900   # 15-min SLA — becomes an asset check
```

```yaml
# dbt.yaml — triggered by replicated_landing_tables completion, not a timer.
type: retail_data_orchestration.components.dbt_project.DbtProjectComponent
attributes:
  project_dir: /workspace/dbt_project
  select: "tag:scenario2"
```

**Concurrency control** (F2.5 overlapping refreshes): every job auto-tags with the run's `partition_key` OR uses Dagster's `RunsFilter` + concurrency key. Add:

```yaml
# concurrency.yaml
type: retail_data_orchestration.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: replicated_landing_tables
  maximum_lag_minutes: 30
```

---

## Scenario 3 — State-aware skip

**Story.** A pipeline reaches a task whose data is already fresh. Decision: skip or run. Two mechanisms — defer to dbt state, or evaluate freshness natively — both demonstrated.

**Defs directory:** `src/retail_data_orchestration/defs/scenario3_state_awareness/`

### Mechanism A — defer to dbt state

```yaml
# dbt_state_reuse.yaml — installs the state-reuse patch that lets Dagster
# treat dbt's `no-op` (state-reuse) status as a successful materialization,
# so a "nothing to build" dbt Cloud response propagates through the graph
# as "task skipped, dependent asset still fresh."
type: retail_data_orchestration.components.dbt_state_reuse_patch.DbtStateReusePatchComponent
attributes: {}
```

```yaml
# dbt_build_selective.yaml — dbt Cloud call with `--select state:modified+`
# so unchanged models produce a `no-op` result rather than a full rebuild.
type: retail_data_orchestration.components.dbt_project.DbtProjectComponent
attributes:
  project_dir: /workspace/dbt_project
  select: "state:modified+"    # dbt state-comparison — real dbt Cloud in prod
  # In real dbt Cloud, this fetches the deferred state artifact automatically.
```

### Mechanism B — decide independently

```yaml
# native_freshness.yaml — Dagster's FreshnessPolicy + AutomationCondition on
# the downstream asset. The orchestrator evaluates staleness against its own
# clock and never calls dbt Cloud to make the decision.
type: retail_data_orchestration.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: mart_daily_summary
  # If the asset materialized within the last 60 min, skip; else run.
  maximum_lag_minutes: 60
```

Combined with an `AutomationCondition` in the YAML:

```yaml
# automation_conditional_mart.yaml
type: retail_data_orchestration.components.event_automation.EventAutomationComponent
attributes:
  asset_key: mart_daily_summary
  automation_condition: >
    {{ dg.AutomationCondition.eager()
       & ~dg.AutomationCondition.newly_materialized_within(hours=1) }}
```

Semantics: run when any parent asset newly materialized (`eager`), *unless* this asset was already materialized within the last hour (skip).

---

## Criteria map — every requirement → the piece of the demo that satisfies it

Rows follow the Ground Rules + Scenario numbering in the source POC document.

### Ground rules (G-series)

| # | Rule | Where satisfied |
|---|---|---|
| G1 | Batch, not real-time | Every scenario's `partition_type` is `daily`; no sub-minute triggers used. |
| G2 | Reuse existing production pipelines | `shell_command_asset` runs the Python extract *unchanged*; the demo does not rewrite it into ops. |
| G3 | Non-interfering | Local mode uses MinIO + DuckDB — nothing writes to production Snowflake. Real-mode YAMLs use dedicated POC schemas. |
| G4 | dbt Cloud, not dbt Core | Local demo uses dbt Core against DuckDB as a stand-in; real-mode swap is one YAML line (see [`POC_REAL_MODE.md`](retail_data_orchestration_real_mode.md)). |
| G5 | Timeboxed | Total scaffold takes ~3 min; each scenario is ~15 lines of YAML. |
| G6 | Demonstrated beats asserted | Every criterion below points at a real file the evaluator can `cat` and inspect. |
| G7 | Love's engineers do the building | Setup script scaffolds the shell; every `defs.yaml` is a template the customer edits — the customer's engineers own the config. |

### Observability (OBS-series)

| # | Requirement | Satisfied by |
|---|---|---|
| OBS-01 | Current state of every step in one screen | Dagster+ UI asset graph. All three scenario chains render as one connected DAG in the UI. |
| OBS-02 | Target tables + last-updated as *data* properties | `external_snowflake_table` (real) / `duckdb_resource` observations (local). Emits `MaterializationEvent` with `last_updated_at`. |
| OBS-03 | SnowPipe load status without opening Snowflake | `snowflake_snowpipe_load_sensor` reads `COPY_HISTORY` → surfaces files-pending / loaded / errored as sensor metadata. |
| OBS-05 | dbt test results as first-class pass/fail | `dbt_project` (both local and real) emits one Dagster **asset check** per dbt test — pass/fail lives on the affected asset, not in run logs. |
| OBS-06 | dbt source freshness against threshold | `dbt_project` `sources.yml` freshness declarations become Dagster `FreshnessPolicy` on the source assets. |
| OBS-07 | Lineage source → mart → reports | Asset graph, cross-code-location deps for the Power BI report step. `dagster-powerbi` `PowerBIWorkspace` (real mode) emits report / dataset assets that depend on the mart. |
| OBS-08 | Lineage onward to consuming reports | Same as OBS-07 — Power BI datasets are first-class assets with lineage back to mart tables. |
| OBS-09 | Run history + duration trend per pipeline | Dagster+ UI runs view + `dagster_runs_to_statsd_sensor` / `dagster_runs_to_otlp_metrics_sensor` (both are community components; wire whichever your metrics backend accepts). |
| OBS-10 | "Is today's data here yet" without an engineering license | Dagster+ Viewer license (unlimited free viewers). Attach a `FreshnessPolicy` to `mart_daily_summary`; stakeholders read the green/red badge in the catalog. |

### Trigger (TRG-series)

| # | Requirement | Satisfied by |
|---|---|---|
| TRG-01 | Trigger on external event (SnowPipe load complete, refresh complete) | `snowflake_snowpipe_load_sensor` (S1) / `hvr_hub_workspace polling_sensor` (S2). |
| TRG-02 | Handle non-deterministic file count | `snowflake_snowpipe_load_sensor` reads `COPY_HISTORY` — cares about *bytes ingested since watermark*, not file count. For truly ambiguous multi-file extracts, the alternative is emit a `_SUCCESS` sentinel; both approaches documented in [`POC_REAL_MODE.md`](retail_data_orchestration_real_mode.md). |
| TRG-03 | Whole chain as one pipeline / one status | Asset graph. `partitioned_asset_launcher_job` for humans + external callers wanting a single status endpoint. |
| TRG-04 | Cron scheduling | `cron_schedule` component wraps `dg.build_schedule_from_partitioned_job` — supports 7 partition types including multi-partitioned (date × static-dim). |
| TRG-05 | Skip-or-run decision on freshness | Scenario 3 above. |
| TRG-06 | dbt Cloud selector-granularity trigger | Selector granularity is configured on the dbt Cloud job itself; `dbt_cloud_trigger_job` supports per-run `steps_override:` (list of dbt commands like `["dbt build --select tag:hourly"]`) to replace the job's default steps for that specific run. |
| TRG-07 | Concurrency control (no overlapping refresh) | Dagster run concurrency + `RunsFilter`. Also `partitioned_asset_launcher_job` enforces partition-level exclusivity. |

### Recovery (REC-series)

| # | Task | Satisfied by |
|---|---|---|
| REC-01 | Re-run only the failed step | Dagster+ UI "re-execute from failed" — platform guarantee, works uniformly across all three scenarios. |
| REC-02 | Re-run with different selector for that run only | `dbt_cloud_trigger_job`'s `steps_override:` field on the component supports one-off selector overrides via Dagster+ Launchpad UI run-config form. For dbt Core (via `dbt_run_job`), `select` is a run-config override. |
| REC-03 | Backfill a specific prior partition | Dagster+ Backfills UI + `partitioned_asset_launcher_job` for config-driven kickoff. |
| REC-04 | Kill in-flight + restart cleanly | Dagster+ UI Terminate + re-launch — platform guarantee. |
| REC-05 | Force a step to succeed / skip | `dagster mark_run_step_successful` CLI + `human_approval_gate` component for gated force-succeed. |
| REC-06 | Retry that fires for infra, not data, failure | `RetryPolicy(max_retries=...)` sets the retry budget uniformly; the *selection* between infra and data errors happens in compute: raise `dagster.RetryRequested(...)` for infra failures (retry consumed), let `dagster.Failure` (or any exception the caller doesn't wrap) propagate for data failures (retry NOT consumed). Wrap each component's shell-out / API-call in a try/except that maps `ConnectionError` / `TimeoutError` / HTTP 5xx to `RetryRequested`. |
| REC-07 | **Consistency of intervention across pipelines** | This is the platform-guarantee vs per-job-design question. Because every scenario uses the same components + Dagster's built-in `RetryPolicy` / `MaterializationEvent` primitives, re-run / cancel / backfill / force-skip semantics are identical across all three. This is the highest-value grade to press vendors on. |

### Integration (INT-series)

| # | Integration | Satisfied by |
|---|---|---|
| INT-01 | dbt Cloud at selector granularity | `dbt_cloud_resource` + `dbt_cloud_trigger_job` for the "Dagster triggers dbt Cloud" direction. `dbt_cloud_job_sensor` for the reverse (Dagster observes dbt Cloud → triggers downstream). Selector granularity via the job's dbt Cloud config, per-run overrides via `steps_override:`. |
| INT-02 | dbt Core support | `dbt_project` component (in the local demo). |
| INT-03 | SnowPipe status → orchestrator | `snowflake_snowpipe_load_sensor`. |
| INT-05 | External replication API (HVR / Fivetran / …) | `hvr_hub_workspace` (real mode). `fivetran_workspace` also available for the Fivetran side of the migration. |
| INT-06 | Stream-processing observability | Out of scope per source doc, but noted: `dagster_runs_to_otlp_sensor` + Kafka `*_to_database_asset` family. |
| INT-07 | Kafka topic health | `kafka_health_sensor` community component observes lag / topic-arrival. |
| INT-08 | Power BI refresh | `dagster-powerbi` official integration (`uv add dagster-powerbi`). Not a community component — official Dagster package. |
| INT-09 | Existing Python job unchanged | `shell_command_asset` — the Python script is invoked as-is via subprocess. |

### Error handling (ERR-series)

| # | Requirement | Satisfied by |
|---|---|---|
| ERR-01 | Legible errors from third-party tools | `snowflake_snowpipe_load_sensor` bubbles `COPY_HISTORY.ERROR_MESSAGE` per-file into asset metadata. `hvr_hub_workspace` bubbles HVR error codes as run failures. |
| ERR-03 | Classify infrastructure vs data failure | Dagster distinguishes `RetryRequested` (infra, consumes a retry from the `RetryPolicy` budget) from `Failure` (data, non-retryable). Classification lives in the compute function itself: a `try/except` around the API call maps transient errors (`ConnectionError`, `TimeoutError`, HTTP 5xx) to `raise dagster.RetryRequested(...)` and lets data errors (schema violation, `Failure`) surface as terminal. `RetryPolicy` itself only sets `max_retries` / `delay` / `backoff` / `jitter` — no built-in error-type filter. |
| ERR-05 | Snowflake dynamic-table fallback-to-full-refresh visibility | `snowflake_table_observation_sensor` component (community) reads `INFORMATION_SCHEMA.DYNAMIC_TABLE_REFRESH_HISTORY` — surfaces incremental-vs-full as sensor metadata. Direct answer to the A3 pain point. |

### Alerting (ALT-series)

| # | Requirement | Satisfied by |
|---|---|---|
| ALT-02 | Missing-run alert (not just failing) | `dagster_missing_run_sensor` community component fires on "expected but did not run." |
| ALT-04 | Stakeholder subscription | Dagster+ Insights subscriptions + `slack_alert` / `teams_alert` community components. |
| ALT-05 | Free viewer access | Dagster+ Viewer license (unlimited free); OBS-10 shared cell above. |

### Development (DEV-series)

| # | Requirement | Satisfied by |
|---|---|---|
| DEV-01 | Templated pipeline creation | This is what community components ARE — every `defs.yaml` is a template with typed placeholders. The `dagster-community-components-cli` `add` command copies a component's files into your project so the template becomes yours. |
| DEV-02 | Standardization | `dg check defs` validates every `defs.yaml` against its component schema at CI time — the standardization is enforced, not aspirational. |
| DEV-03 | Git-based promotion + PR-level preview | Dagster+ Branch Deployments — one branch = one preview deployment. |
| DEV-04 | Rollback | Git revert + Dagster+ deployment rollback UI. |
| DEV-05 | Dev/QA/prod separation | Dagster+ multi-deployment (each env is its own deployment; code locations move via git). |
| DEV-07 | Audit trail | Dagster+ Audit Log — every re-run, config override, and permission change stamped with actor + timestamp. |

### Operations (OPS-series)

| # | Requirement | Satisfied by |
|---|---|---|
| OPS-01 | Time to productivity | `dagster-component add` + copy an existing scenario dir + edit slots — the second pipeline of a shape is ~10 min of YAML editing, not a rewrite. |
| OPS-04 | Cost of stakeholder access | Dagster+ free Viewer license. |

---

## What's out of scope for this demo

- Rewriting the Python extract to emit a `_SUCCESS` sentinel. Documented as an option in [`POC_REAL_MODE.md`](retail_data_orchestration_real_mode.md) under TRG-02, but the demo's default path is `snowflake_snowpipe_load_sensor` which reads `COPY_HISTORY` — the sentinel approach is only needed if the source APIs go through a non-SnowPipe load path.
- Real credentials / real Snowflake / real dbt Cloud / real HVR. The demo is deliberately laptop-runnable; the swap to real is one commit's worth of YAML edits documented in [`POC_REAL_MODE.md`](retail_data_orchestration_real_mode.md).
- Stream-processing observability (source doc marks it out of scope).
- Kafka health (source doc marks it observed-not-orchestrated).

---

## Files this demo scaffolds

```
retail-data-orchestration/
├── docker-compose.yml                     — MinIO for the object-store hop
├── dbt_project/                           — dbt-core against DuckDB (dbt Cloud stand-in)
│   ├── dbt_project.yml
│   ├── profiles.yml
│   └── models/
│       ├── scenario1/
│       ├── scenario2/
│       └── scenario3/
├── extract_scripts/
│   ├── api_extract.py                     — the "existing Python job" stand-in (writes N gz-JSON files)
│   └── mock_replication_refresh.py        — stand-in HVR call
├── src/retail_data_orchestration/
│   ├── components/                        — component files copied into the project
│   │   ├── shell_command_asset/
│   │   ├── filesystem_monitor/
│   │   ├── dbt_project/
│   │   ├── freshness_check/
│   │   ├── event_automation/
│   │   └── dbt_state_reuse_patch/
│   └── defs/
│       ├── scenario1_api_load_dbt/
│       │   ├── extract.yaml
│       │   ├── load_completion_sensor.yaml
│       │   └── dbt.yaml
│       ├── scenario2_replication_refresh/
│       │   ├── refresh.yaml
│       │   ├── dbt.yaml
│       │   └── concurrency.yaml
│       └── scenario3_state_awareness/
│           ├── dbt_state_reuse.yaml
│           ├── dbt_build_selective.yaml
│           ├── native_freshness.yaml
│           └── automation_conditional_mart.yaml
├── POC_REAL_MODE.md                       — the swap-guide from local → real Snowflake/dbt Cloud/HVR
│                                            (identical content also lives at
│                                             examples/retail_data_orchestration_real_mode.md)
└── README.md                              — quickstart + `dg dev` next steps
```

**Next**: after `./setup.sh` finishes, `cd retail-data-orchestration && dg dev` and open http://localhost:3000. The three scenario asset groups appear in the catalog. Materialize `raw_api_files` in scenario 1, watch the load-completion sensor fire, watch dbt kick off. That's the whole triggered-on-evidence-not-time loop, on a laptop, in ~30 seconds.
