# Retail Data Orchestration — Real POC mode swap guide

Companion to [retail_data_orchestration.md](retail_data_orchestration.md) — the local demo scaffolded by `setup_retail_data_orchestration_demo.sh` uses laptop-friendly stand-ins for every credentialled system (Snowflake, dbt Cloud, HVR, Power BI). This doc walks each stand-in → real component YAML swap.

**How to use this doc.** In the local scaffold, every stand-in YAML file has a `# LOCAL:` / `# REAL:` comment block pointing at the swap. Copy the "REAL" block from the section below over the "LOCAL" block, add the required env vars to your `.env`, run `dagster-component add <new component id>` if a new component is needed, and `dg check defs` to validate.

Each swap is intentionally isolated — one YAML file per swap. You can convert one scenario without touching the other two.

**Estimated conversion effort**: 15 minutes per scenario once credentials + endpoints are known.

---

## Scenario 1 — API extract → SnowPipe load → dbt Cloud → mart

### 1a. Snowflake surface: `filesystem_monitor` → `snowflake_workspace` (RECOMMENDED)

**Prefer the workspace-shape component** over piecemeal individuals. One `snowflake_workspace` block discovers every Snowpipe + landing table + mart table under a given database/schema and emits them all as first-class Dagster assets. A `polling_sensor: true` on the same component watches for pipe-load completions and emits `AssetObservation` events — the same signal the piecemeal `snowflake_snowpipe_load_sensor` gives you, but bundled with the rest of the Snowflake surface.

```bash
dagster-component add snowflake_workspace
```

Replace `src/<pkg>/defs/scenario1_api_load_dbt/load_completion_sensor.yaml` with a new `snowflake_workspace.yaml` (and delete the old `filesystem_monitor` YAML):

```yaml
type: <pkg>.components.snowflake_workspace.SnowflakeWorkspaceComponent
attributes:
  workspace:
    account:              {env: SNOWFLAKE_ACCOUNT}
    user:                 {env: SNOWFLAKE_USER}
    authenticator:        SNOWFLAKE_JWT
    private_key_path:     {env: SNOWFLAKE_PRIVATE_KEY_FILE}
    warehouse:            COMPUTE_WH
    database:             FUEL_AND_TRADING
    schema:               LANDING
    role:                 SYSADMIN
  import_snowpipes:          true    # ← the load-completion primitive
  import_tables:             true    # ← landing + mart tables in one shot
  import_dynamic_tables:     false
  import_stored_procedures:  false
  polling_sensor:            true    # emits AssetObservation on pipe-load completion
  poll_interval_seconds:     60
  group_name: scenario1_snowpipe_dbt
  kinds: [snowflake, snowpipe]
```

**Why the workspace is the right primitive for the POC:**
- One YAML block covers the whole Snowflake surface: Snowpipes, landing tables, mart tables, dynamic tables (if enabled), stored procedures (if enabled). Scales without adding more YAML per object.
- Auto-discovers new objects: land a new `EXTRACT_<endpoint>_PIPE` in Snowflake, restart Dagster, and it's a first-class asset with no config change.
- The polling sensor handles TRG-01 + TRG-02 + OBS-03 in one — `COPY_HISTORY` per-file metadata bubbles into the asset observation events, and the "how many files am I waiting for?" question goes away because the sensor triggers on *load events*, not file count.

### 1a-alt. Piecemeal — `snowflake_snowpipe_load_sensor` (for finer control)

Reach for the piecemeal components only when the workspace's defaults don't fit:

- Per-pipe rate limiting or custom `minimum_interval_seconds`
- Per-object custom `AssetSpec` attributes (owners, tags, freshness policies)
- Explicit target-job specification (workspace emits assets; if you need a *job* triggered instead of asset materialization events, the piecemeal sensor is the right tool)

If any of those apply:

```bash
dagster-component add snowflake_snowpipe_load_sensor snowflake_snowpipe external_snowflake_table
```

Replace `src/<pkg>/defs/scenario1_api_load_dbt/load_completion_sensor.yaml`:

```yaml
type: <pkg>.components.snowflake_snowpipe_load_sensor.SnowflakeSnowpipeLoadSensorComponent
attributes:
  sensor_name: api_load_complete_sensor
  job_name: dbt_build_scenario1_job

  pipe_name: FUEL_AND_TRADING_PIPE       # your real pipe name
  destination_table: LANDING.RAW_API_EVENTS

  account:     "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user:        "{{ env('SNOWFLAKE_USER') }}"
  warehouse:   COMPUTE_WH
  database:    FUEL_AND_TRADING
  schema:      LANDING
  role:        SYSADMIN

  authenticator: SNOWFLAKE_JWT
  private_key_file: "{{ env('SNOWFLAKE_PRIVATE_KEY_FILE') }}"

  minimum_interval_seconds: 60
  lookback_minutes: 60
  pass_file_metadata: true
  default_status: running
```

**When the piecemeal path is better** vs the workspace:
- Reads Snowflake's `COPY_HISTORY` view directly with more granular filtering — files-pending / files-loaded / files-errored / bytes / last-load-time surface as sensor metadata (OBS-03).
- Triggers a specific Dagster JOB by name, rather than emitting an AssetObservation event.
- Per-file failure metadata is finer-grained than the workspace's per-batch aggregation.

### 1b. Python extract path

Same `shell_command_asset` component — only the `command:` changes. Point at your real extract script's install path and switch its output flag from `--out $PROJECT_ABS/data/incoming` (local MinIO) to your real S3 URI. No new component install needed.

```yaml
type: <pkg>.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: raw_api_files
  command: "python /workspace/electricera-extract/extract.py --out s3://loves-electricera/snowpipe/..."
  group_name: scenario1_api_load_dbt
  kinds: [python, api]
  partition_type: daily              # add if you want per-day backfill grain
  partition_start: "2026-01-01"
```

### 1c. dbt: local shell-out → `dbt_cloud_resource` + `dbt_cloud_trigger_job`

Three components involved — one registers the connection, one triggers dbt Cloud (op-job you install), one optionally observes dbt Cloud (reverse direction):

- **`dbt_cloud_resource`** — registers `dagster_dbt.DbtCloudResource` for the connection.
- **`dbt_cloud_trigger_job`** — Dagster **op-job** that calls `DbtCloudResource.run_job_and_poll(job_id=...)`. Sensors / schedules target this by name.
- **`dbt_cloud_job_sensor`** — REVERSE direction: watches dbt Cloud, triggers a Dagster job when a dbt Cloud run completes. Useful for chaining downstream Dagster work off dbt Cloud completions (e.g., report refresh). Skip unless you need the observation direction.

**Do NOT use `dbt_run_job` for dbt Cloud** — that component runs dbt Core via subprocess, not dbt Cloud.

```bash
dagster-component add dbt_cloud_resource dbt_cloud_trigger_job
```

Register the resource (once per code location):

```yaml
# resources/dbt_cloud_resource.yaml
type: <pkg>.components.dbt_cloud_resource.DbtCloudResourceComponent
attributes:
  resource_key:       dbt_cloud_resource
  auth_token_env_var: DBT_CLOUD_API_TOKEN
  account_id:         12345          # your dbt Cloud account ID (int, not env)
```

Replace `src/<pkg>/defs/scenario1_api_load_dbt/dbt.yaml`:

```yaml
type: <pkg>.components.dbt_cloud_trigger_job.DbtCloudTriggerJobComponent
attributes:
  job_name: dbt_build_scenario1_job              # sensors target this string
  dbt_cloud_job_id: 67890                        # your dbt Cloud job ID (int)
  dbt_cloud_resource_key: dbt_cloud_resource     # matches the resource above
  wait_for_completion: true
  poll_interval_seconds: 30
  cause: "Triggered by Dagster on Snowpipe load event"

  # After successful run, emit AssetMaterialization for each mart. This
  # is what wires the dbt Cloud run into Dagster's asset graph.
  emit_materializations_for:
    - snowflake_workspace/tables/MART/DAILY_SUMMARY
    - snowflake_workspace/tables/MART/HOURLY_PRICING
```

Wire the SnowPipe load sensor to this job (from 1a — the sensor's `job_name:` field targets `dbt_build_scenario1_job`).

**Optional — observe dbt Cloud runs too.** If you also want a Dagster job to fire when the dbt Cloud run *completes* (e.g., for downstream Power BI refresh), add `dbt_cloud_job_sensor`. Note the correct field names:

```yaml
type: <pkg>.components.dbt_cloud_job_sensor.DbtCloudJobSensorComponent
attributes:
  sensor_name:       dbt_cloud_status_sensor
  account_id:        12345                       # int, not env
  job_id:            67890                       # int, not env
  api_token_env_var: DBT_CLOUD_API_TOKEN         # env-var NAME (string), not value
  job_name:          powerbi_refresh_job         # existing Dagster job to trigger
  minimum_interval_seconds: 60
  default_status:    running
```

**What this gets you**: per-model asset-check pass/fail from dbt Cloud (OBS-05) surface as Dagster asset checks on the mart tables. Selector-granularity is configured on the dbt Cloud side; override per-run via `steps_override:` on the trigger component. Source freshness (OBS-06) is a dbt-side capability that appears in dbt Cloud UI and can be surfaced in Dagster by materializing the source asset with per-source freshness policies.

### 1d. External Snowflake tables (OBS-02) — already covered by the workspace

If you took the recommended workspace path in 1a, `import_tables: true` already emits every mart table as a first-class Dagster asset. Skip this section.

If you took the piecemeal path in 1a-alt, add `external_snowflake_table` one per mart table:

```bash
dagster-component add external_snowflake_table
```

```yaml
type: <pkg>.components.external_snowflake_table.ExternalSnowflakeTableComponent
attributes:
  database: FUEL_AND_TRADING
  schema:   MART
  table:    daily_summary
```

Either way, OBS-02 is satisfied — last-updated + row count show up as *properties of the data*, not just as run history.

### 1e. Power BI refresh (S1.8 stretch)

Use the OFFICIAL `dagster-powerbi` integration (not a community component). Add:

```bash
uv add dagster-powerbi
```

Wire in your project's root `definitions.py`:

```python
from dagster_powerbi import PowerBIWorkspace, PowerBIToken

powerbi = PowerBIWorkspace(
    credentials=PowerBIToken(api_token="{{ env('POWERBI_TOKEN') }}"),
    workspace_id="{{ env('POWERBI_WORKSPACE_ID') }}",
)
# Then include powerbi.build_defs() in your Definitions.
```

`dagster-powerbi` emits report / dataset / semantic-model assets that hang off your Snowflake mart tables — one asset graph, source → mart → Power BI (OBS-07 + OBS-08).

---

## Scenario 2 — External replication refresh → dbt Cloud

### 2a. Refresh: `shell_command_asset` → `hvr_hub_workspace`

```bash
dagster-component add hvr_hub_workspace
```

Replace `src/<pkg>/defs/scenario2_replication_refresh/refresh.yaml`:

```yaml
type: <pkg>.components.hvr_hub_workspace.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"          # e.g. https://hvr-hub.internal:4340
    hub_name: "{{ env.HVR_HUB_NAME }}"         # e.g. prod_hub
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
    api_version: "v6.3.5"                       # pin for stability

  channel_selector:
    by_pattern: ["fuel_price*"]

  action: refresh                               # POST /channels/{c}/refresh + poll
  wait_for_completion: true
  poll_interval_seconds: 30
  timeout_seconds: 1800

  polling_sensor: true                          # AssetObservation per (channel × target × table)
  observation_interval_seconds: 300

  # 15-min SLA — becomes an asset check that fails when integrate-lag exceeds threshold
  freshness_lag_threshold_seconds: 900

  group_name: scenario2_replication_refresh
  kinds: [hvr, cdc]
```

**What this gets you** vs. the local `shell_command_asset` stand-in:
- Refresh is a visible node with its own state, duration, and error surface (5.5 req #4) — not an opaque "call an API" task.
- Per-table completion (OBS-03) — HVR exposes per-table state; the component surfaces it as `AssetObservation` per table.
- Partial-completion detection (F2.2) — the freshness-lag asset check fails when some tables are behind.

### 2b. dbt: same swap as 1c.

Add a second `dbt_cloud_trigger_job` for scenario 2 (reuses the `dbt_cloud_resource` from 1c):

```yaml
type: <pkg>.components.dbt_cloud_trigger_job.DbtCloudTriggerJobComponent
attributes:
  job_name: dbt_build_scenario2_job
  dbt_cloud_job_id: 67891
  dbt_cloud_resource_key: dbt_cloud_resource
  wait_for_completion: true
  cause: "Triggered by Dagster on HVR channel refresh completion"
  emit_materializations_for:
    - snowflake_workspace/tables/MART/FUEL_PRICES_MART
```

Trigger it via an `AutomationCondition` on the downstream mart (HVR emits materializations on channel refresh — no separate sensor needed, unlike scenario 1 where Snowpipe emits observations, not materializations):

```yaml
type: <pkg>.components.event_automation.EventAutomationComponent
attributes:
  asset_key: snowflake_workspace/tables/MART/FUEL_PRICES_MART
  automation_condition: "{{ dg.AutomationCondition.eager() & ~dg.AutomationCondition.newly_materialized_within(minutes=30) }}"
```

---

## Scenario 3 — State-aware skip

Mechanism B (native `FreshnessPolicy` + `AutomationCondition`) works locally unchanged. Only Mechanism A (defer to dbt state) needs a real-mode addition:

### 3a. Mechanism A — dbt state-reuse patch

```bash
dagster-component add dbt_state_reuse_patch dbt_run_job
```

Add `src/<pkg>/defs/scenario3_state_awareness/dbt_state_reuse.yaml`:

```yaml
# Bridge patch — monkey-patches dagster-dbt at code-location load to treat
# dbt Cloud's `no-op` (state-reuse) response as a successful
# materialization event. Otherwise "nothing to build" surfaces as a
# Dagster failure.
type: <pkg>.components.dbt_state_reuse_patch.DbtStateReusePatchComponent
attributes: {}
```

And `src/<pkg>/defs/scenario3_state_awareness/dbt_build_selective.yaml`:

```yaml
type: <pkg>.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario3_stateaware
  command: build
  select: "state:modified+"    # dbt state-comparison — dbt Cloud fetches the deferred state artifact automatically
```

Semantics: dbt Cloud compares the current manifest to the last successful production manifest and only builds models whose SQL / config / upstream schema changed. Unchanged models return `status: "no-op"`, which the patch translates into a Dagster materialization event so the asset stays green ("data is fresh, task correctly skipped").

---

## Env vars checklist

Set these in a project-root `.env` file — Dagster loads it automatically via `dg dev`.

```bash
# Snowflake
SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=DAGSTER_SVC
SNOWFLAKE_PRIVATE_KEY_FILE=/secrets/svc.p8

# dbt Cloud
DBT_CLOUD_ACCOUNT_ID=12345
DBT_CLOUD_API_TOKEN=dbtc_...
DBT_CLOUD_JOB_ID_SCENARIO1=67890

# HVR (Scenario 2)
HVR_HUB_URL=https://hvr-hub.internal:4340
HVR_HUB_NAME=prod_hub
HVR_USERNAME=svc_dagster
HVR_PASSWORD=...

# Power BI (S1.8 stretch)
POWERBI_TOKEN=...
POWERBI_WORKSPACE_ID=...
```

---

## Validation before going live

After every swap, run:

```bash
uv run dg check defs
```

This walks every `defs.yaml`, validates against the installed component's `schema.json`, and confirms all `required_resource_keys` are wired. It's the same check the demo's automated CI runs on every PR.

For a full smoke test without materializing:

```bash
uv run dg launch --assets '*' --dry-run
```

Dry-run planner shows the exact steps a real materialization would execute, including which sensors will fire and which resources will be initialized. Catches missing env vars before you burn a real Snowflake warehouse credit.

---

## Companion docs

- [retail_data_orchestration.md](retail_data_orchestration.md) — main walkthrough with the full criteria mapping table
- [retail_data_orchestration_real_from_scratch.md](retail_data_orchestration_real_from_scratch.md) — hands-on REAL POC tutorial (build against real Snowflake / dbt Cloud / HVR / Power BI from an empty terminal, using `snowflake_workspace` as the primary Snowflake surface)
- [retail_data_orchestration_from_scratch.md](retail_data_orchestration_from_scratch.md) — hands-on LOCAL tutorial (build the local stand-in demo step-by-step)
- [setup_retail_data_orchestration_demo.sh](setup_retail_data_orchestration_demo.sh) — the local scaffold script; run this first, then follow this real-mode guide
