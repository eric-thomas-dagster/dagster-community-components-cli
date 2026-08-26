# Retail Data Orchestration — Real POC mode swap guide

Companion to [retail_data_orchestration.md](retail_data_orchestration.md) — the local demo scaffolded by `setup_retail_data_orchestration_demo.sh` uses laptop-friendly stand-ins for every credentialled system (Snowflake, dbt Cloud, HVR, Power BI). This doc walks each stand-in → real component YAML swap.

**How to use this doc.** In the local scaffold, every stand-in YAML file has a `# LOCAL:` / `# REAL:` comment block pointing at the swap. Copy the "REAL" block from the section below over the "LOCAL" block, add the required env vars to your `.env`, run `dagster-component add <new component id>` if a new component is needed, and `dg check defs` to validate.

Each swap is intentionally isolated — one YAML file per swap. You can convert one scenario without touching the other two.

**Estimated conversion effort**: 15 minutes per scenario once credentials + endpoints are known.

---

## Scenario 1 — API extract → SnowPipe load → dbt Cloud → mart

### 1a. Load-completion sensor: `filesystem_monitor` → `snowflake_snowpipe_load_sensor`

Add the component's files to the project first:

```bash
dagster-component add snowflake_snowpipe_load_sensor
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

**What this gets you** vs. the local `filesystem_monitor`:
- Reads Snowflake's `COPY_HISTORY` view directly — files-pending / files-loaded / files-errored / bytes / last-load-time surface as sensor metadata (satisfies OBS-03 without opening a Snowflake worksheet).
- Handles the non-deterministic file count (TRG-02) — it triggers on *load events*, not on file arrival, so it doesn't need to know how many files the extract emits.
- Per-file failure metadata: `COPY_HISTORY.ERROR_MESSAGE` bubbles into the sensor tick log (F1.2 — malformed file → visible SnowPipe error).

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

### 1c. dbt: local `dbt_project` shell-out → `dbt_run_job` + `dbt_cloud_job_sensor`

```bash
dagster-component add dbt_run_job dbt_cloud_job_sensor
```

Replace `src/<pkg>/defs/scenario1_api_load_dbt/dbt.yaml`:

```yaml
type: <pkg>.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario1_job
  # This is a Dagster job; the SnowPipe sensor above triggers it. You
  # CAN also add a scheduled sibling for calendar-driven runs.
```

Add a sibling `dbt_cloud_job_sensor.yaml`:

```yaml
type: <pkg>.components.dbt_cloud_job_sensor.DbtCloudJobSensorComponent
attributes:
  sensor_name: dbt_cloud_status_sensor
  dbt_cloud_job_id: "{{ env('DBT_CLOUD_JOB_ID_SCENARIO1') }}"
  dbt_cloud_api_token: "{{ env('DBT_CLOUD_API_TOKEN') }}"
  dbt_cloud_account_id: "{{ env('DBT_CLOUD_ACCOUNT_ID') }}"
  minimum_interval_seconds: 60
```

**What this gets you**: per-model asset-check pass/fail (OBS-05), selector-granularity trigger (INT-01 / TRG-06), dbt source freshness surfaced (OBS-06).

### 1d. External Snowflake tables (OBS-02)

```bash
dagster-component add external_snowflake_table
```

Add one per mart table (repeat for each `rpt_<endpoint>` in scope):

```yaml
type: <pkg>.components.external_snowflake_table.ExternalSnowflakeTableComponent
attributes:
  database: FUEL_AND_TRADING
  schema:   MART
  table:    daily_summary
```

This makes the mart tables first-class Dagster assets. Last-updated + row count show up as *properties of the data* (OBS-02), not just as run history.

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

`dbt_run_job` + `dbt_cloud_job_sensor` — replace the `shell_command_asset` shim with `dbt_run_job`. The `deps: [replicated_landing_tables]` line stays; `replicated_landing_tables` now becomes an asset produced by `hvr_hub_workspace` instead of the shell shim.

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
- [retail_data_orchestration_from_scratch.md](retail_data_orchestration_from_scratch.md) — hands-on tutorial (build the local demo step-by-step without the scaffold script)
- [setup_retail_data_orchestration_demo.sh](setup_retail_data_orchestration_demo.sh) — the scaffold script; run this first, then follow this real-mode guide
