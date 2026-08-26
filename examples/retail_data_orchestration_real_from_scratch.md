# Retail Data Orchestration — REAL POC from-scratch tutorial

For readers building the actual production-shape POC against real Snowflake / dbt Cloud / HVR / Power BI. No stand-ins, no MinIO, no DuckDB. Starts from an empty terminal and ends in a working POC deployment.

> **Companion docs — which one to use.**
> - **This doc** — you have credentials and want to build the REAL POC by hand, understanding each piece.
> - **[Local from-scratch tutorial](retail_data_orchestration_from_scratch.md)** — you want to prototype without credentials first (dbt Core / DuckDB / MinIO stand-ins).
> - **[Main walkthrough](retail_data_orchestration.md)** — reference + criteria mapping table.
> - **[Real-mode swap guide](retail_data_orchestration_real_mode.md)** — you already have the local demo working and want the fastest-path convert.

---

## Prerequisites

Access (any equivalent tool works — swap components accordingly):

- **Snowflake** — account with `USAGE` on a warehouse + `SELECT` / `INSERT` on target schemas + `MONITOR` on the PIPE(s). Keypair auth recommended (`SNOWFLAKE_JWT`).
- **dbt Cloud** — API token + account ID + at least one job ID.
- **HVR Hub 6.x** (for scenario 2) — reachable REST endpoint + service account with `refresh` permissions.
- **Power BI** (optional S1.8 stretch) — service principal or delegated token + workspace ID.

Local tooling:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version && python3 --version   # Python 3.10+
```

---

## Design decision — workspace vs piecemeal

You have two ways to bring Snowflake objects into Dagster:

**Option A — `snowflake_workspace` (recommended for the POC).** One component discovers every Snowpipe / table / dynamic table / stream / stored procedure / etc. in a database or schema and emits them all as first-class Dagster assets. Includes a `polling_sensor` that observes task + pipe completions Fivetran-style. Best when you want lineage across the whole warehouse without one-YAML-per-object.

**Option B — piecemeal individual components.** `snowflake_snowpipe`, `snowflake_snowpipe_load_sensor`, `external_snowflake_table` — one YAML per object. Best when you need per-object customization (per-pipe rate limits, per-table freshness policies, custom asset key prefixes).

This tutorial uses **Option A** as the primary path. The [real-mode swap guide](retail_data_orchestration_real_mode.md) covers Option B in detail for cases where the workspace's defaults don't fit.

---

## Step 1: Bootstrap the Dagster project

```bash
uvx create-dagster project retail-data-orchestration --uv-sync
cd retail-data-orchestration
export PROJECT_ABS="$(pwd)"
```

```bash
uv run dg check defs
```

Expected:

```
All component YAML validated successfully.
All definitions loaded successfully.
```

---

## Step 2: Install the components you need

Same alias pattern as the local tutorial:

```bash
alias dcc="uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git dagster-component"
```

Install the six components the real POC uses:

```bash
dcc add snowflake_workspace   --auto-install     # ← Scenario 1 primary: all Snowflake objects
dcc add dbt_run_job           --auto-install     # dbt Cloud trigger
dcc add dbt_cloud_job_sensor  --auto-install     # dbt Cloud observation
dcc add dbt_state_reuse_patch --auto-install     # Scenario 3 Mechanism A
dcc add hvr_hub_workspace     --auto-install     # Scenario 2
dcc add freshness_check       --auto-install     # freshness SLAs + Scenario 3 Mechanism B
```

Also install the OFFICIAL Power BI integration (not a community component — Dagster's own package):

```bash
uv add dagster-powerbi
```

Clean up the placeholder `defs.yaml` files that `dagster-component add` creates — we're writing scenario-organized YAML below:

```bash
find src/retail_data_orchestration/defs -maxdepth 2 -name defs.yaml -delete
```

---

## Step 3: Credentials in a `.env`

Dagster loads `.env` automatically. Put every credential in one place:

```bash
cat > .env <<'ENV'
# ── Snowflake ────────────────────────────────────────────────────
SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=DAGSTER_SVC
SNOWFLAKE_PRIVATE_KEY_FILE=/secrets/dagster_svc.p8
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=SYSADMIN

# Scenario 1 databases
SNOWFLAKE_DATABASE_S1=FUEL_AND_TRADING
SNOWFLAKE_SCHEMA_S1_LANDING=LANDING
SNOWFLAKE_SCHEMA_S1_MART=MART

# Scenario 2 databases
SNOWFLAKE_DATABASE_S2=FUEL_PRICES
SNOWFLAKE_SCHEMA_S2_LANDING=LANDING

# ── dbt Cloud ────────────────────────────────────────────────────
DBT_CLOUD_ACCOUNT_ID=12345
DBT_CLOUD_API_TOKEN=dbtc_xxxxxxxxxxxx
DBT_CLOUD_JOB_ID_SCENARIO1=67890
DBT_CLOUD_JOB_ID_SCENARIO2=67891
DBT_CLOUD_JOB_ID_SCENARIO3=67892

# ── HVR Hub (Scenario 2) ─────────────────────────────────────────
HVR_HUB_URL=https://hvr-hub.internal:4340
HVR_HUB_NAME=prod_hub
HVR_USERNAME=svc_dagster
HVR_PASSWORD=xxxxxxxxxxxx

# ── Power BI (S1.8 stretch) ──────────────────────────────────────
POWERBI_TENANT_ID=00000000-0000-0000-0000-000000000000
POWERBI_CLIENT_ID=00000000-0000-0000-0000-000000000000
POWERBI_CLIENT_SECRET=xxxxxxxxxxxx
POWERBI_WORKSPACE_ID=00000000-0000-0000-0000-000000000000
ENV
echo ".env" >> .gitignore
```

Verify no secret leaked:

```bash
git check-ignore .env    # should print `.env` (it's ignored) — exit 0 = OK
```

---

## Step 4: Scenario 1 — Snowflake workspace + dbt Cloud

This is the big one. One `snowflake_workspace` block gives you every Snowflake object (Snowpipes, landing tables, mart tables, stored procs — whatever you enable) as first-class Dagster assets. A polling sensor turns pipe-load completions into materialization events. dbt Cloud runs on those events.

```bash
mkdir -p src/retail_data_orchestration/defs/scenario1_snowpipe_dbt
```

### 4a. `snowflake_workspace.yaml` — one YAML, every Snowflake object

```bash
cat > src/retail_data_orchestration/defs/scenario1_snowpipe_dbt/snowflake_workspace.yaml <<'YAML'
type: retail_data_orchestration.components.snowflake_workspace.SnowflakeWorkspaceComponent
attributes:
  # The workspace: block IS a dagster_snowflake.SnowflakeResource — the
  # same shape as dagster-fivetran / dagster-databricks / dagster-powerbi
  # workspaces. Keypair auth via SNOWFLAKE_JWT + private_key_file.
  workspace:
    account:              {env: SNOWFLAKE_ACCOUNT}
    user:                 {env: SNOWFLAKE_USER}
    authenticator:        SNOWFLAKE_JWT
    private_key_path:     {env: SNOWFLAKE_PRIVATE_KEY_FILE}
    warehouse:            {env: SNOWFLAKE_WAREHOUSE}
    database:             {env: SNOWFLAKE_DATABASE_S1}
    schema:               {env: SNOWFLAKE_SCHEMA_S1_LANDING}
    role:                 {env: SNOWFLAKE_ROLE}

  # What to import as Dagster assets. Toggle each on if the POC scope
  # includes it. For scenario 1 the essentials are:
  import_snowpipes:          true    # ← the load-completion primitive
  import_tables:             true    # ← landing tables + mart tables
  import_dynamic_tables:     false
  import_stored_procedures:  false
  import_streams:            false
  import_materialized_views: false
  import_stages:             false
  import_alerts:             false
  import_tasks:              false

  # polling_sensor observes task + Snowpipe completions and emits
  # AssetObservation events. This is the "load happened" signal that
  # gates the downstream dbt build — no more hardcoded 2-minute delay.
  polling_sensor:            true
  poll_interval_seconds:     60

  group_name: scenario1_snowpipe_dbt
  kinds: [snowflake, snowpipe]
YAML
```

**What this discovers.** On code-location load, Dagster queries Snowflake's `INFORMATION_SCHEMA` and enumerates every pipe + table under the given database + schema. Each becomes a Dagster asset. Zero YAML per object.

**Recommendation for the POC.** Point at the POC's isolated landing + mart schemas (G3 non-interfering). If you want per-object filtering, add:

```yaml
# under attributes:
name_filter:
  include: ["*FUEL*", "*API_EVENTS*"]
```

### 4b. `dbt_cloud_job.yaml` — trigger dbt Cloud at selector granularity

```bash
cat > src/retail_data_orchestration/defs/scenario1_snowpipe_dbt/dbt_cloud_job.yaml <<'YAML'
type: retail_data_orchestration.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario1_job
  # dbt_run_job kicks off dbt Cloud's job via the Cloud API. Selector
  # granularity comes from the dbt Cloud job definition itself; if you
  # need to override per-run, pass `select:` as run config.
YAML
```

### 4c. `dbt_cloud_sensor.yaml` — trigger the dbt job on Snowpipe-load events

The `snowflake_workspace` polling sensor emits `AssetObservation` events when Snowpipes complete loads. `dbt_cloud_job_sensor` watches for those events and triggers the dbt Cloud job.

```bash
cat > src/retail_data_orchestration/defs/scenario1_snowpipe_dbt/dbt_cloud_sensor.yaml <<'YAML'
type: retail_data_orchestration.components.dbt_cloud_job_sensor.DbtCloudJobSensorComponent
attributes:
  sensor_name: dbt_cloud_scenario1_sensor
  dbt_cloud_job_id:    {env: DBT_CLOUD_JOB_ID_SCENARIO1}
  dbt_cloud_api_token: {env: DBT_CLOUD_API_TOKEN}
  dbt_cloud_account_id: {env: DBT_CLOUD_ACCOUNT_ID}

  # Only surface test failures as first-class asset check pass/fail
  # (OBS-05); model successes emit MaterializationEvents which the mart
  # assets from snowflake_workspace pick up as freshness signals (OBS-02).
  minimum_interval_seconds: 60
  default_status: running

  # Optional: filter which upstream materializations trigger this sensor.
  # Omit to trigger on ANY monitored asset materializing.
  triggering_assets:
    - snowflake_workspace/snowpipes/FUEL_AND_TRADING_LANDING_PIPE
YAML
```

Validate:

```bash
uv run dg check defs
```

---

## Step 5: Scenario 2 — HVR full-refresh → dbt Cloud

```bash
mkdir -p src/retail_data_orchestration/defs/scenario2_replication_refresh
```

### 5a. `hvr_workspace.yaml` — HVR channels as Dagster assets

Same workspace-shape pattern as Snowflake. One YAML block discovers every channel + replicated table on the HVR Hub and materializes them on demand (or continuously, depending on the channel type).

```bash
cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/hvr_workspace.yaml <<'YAML'
type: retail_data_orchestration.components.hvr_hub_workspace.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:     {env: HVR_HUB_URL}
    hub_name:    {env: HVR_HUB_NAME}
    username:    {env: HVR_USERNAME}
    password:    {env: HVR_PASSWORD}
    api_version: v6.3.5

  # Filter to just the full-refresh channels — CDC channels are
  # continuous and don't need Dagster orchestration.
  channel_selector:
    by_pattern: ["fuel_price*"]

  # On materialize, POST /channels/{c}/refresh and poll to completion.
  # `wait_for_completion: true` blocks until the refresh is truly done
  # (vs "API accepted → assume success" — see F2.1 in the scorecard).
  action: refresh
  wait_for_completion: true
  poll_interval_seconds: 30
  timeout_seconds: 1800

  # Polling sensor emits AssetObservation per (channel × target × table)
  # every N seconds — surfaces integrate lag as sensor metadata.
  polling_sensor: true
  observation_interval_seconds: 300

  # 15-min freshness SLA — becomes an asset check that fails when the
  # last observed integrate lag exceeds the threshold. Wire into
  # alerts + branch-deploy gates.
  freshness_lag_threshold_seconds: 900

  group_name: scenario2_replication_refresh
  kinds: [hvr, cdc]
YAML
```

### 5b. dbt Cloud trigger — same shape as scenario 1

```bash
cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/dbt_cloud_job.yaml <<'YAML'
type: retail_data_orchestration.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario2_job
YAML

cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/dbt_cloud_sensor.yaml <<'YAML'
type: retail_data_orchestration.components.dbt_cloud_job_sensor.DbtCloudJobSensorComponent
attributes:
  sensor_name: dbt_cloud_scenario2_sensor
  dbt_cloud_job_id:     {env: DBT_CLOUD_JOB_ID_SCENARIO2}
  dbt_cloud_api_token:  {env: DBT_CLOUD_API_TOKEN}
  dbt_cloud_account_id: {env: DBT_CLOUD_ACCOUNT_ID}
  minimum_interval_seconds: 60
  default_status: running
  # Trigger on the HVR channel completing its refresh — the specific
  # asset key depends on your channel_selector. Check `dg list defs`
  # after first load-time for the exact key.
  triggering_assets:
    - hvr/prod_hub/fuel_price02
YAML
```

Validate:

```bash
uv run dg check defs
```

---

## Step 6: Scenario 3 — state-aware skip

Both mechanisms in the source doc:

### 6a. Mechanism A — dbt state-reuse patch

```bash
mkdir -p src/retail_data_orchestration/defs/scenario3_state_awareness
```

```bash
cat > src/retail_data_orchestration/defs/scenario3_state_awareness/dbt_state_reuse_patch.yaml <<'YAML'
# Bridge patch — makes dagster-dbt treat dbt Cloud's `no-op` (state-reuse)
# response as a successful materialization event. Without this, dbt Cloud
# returning "nothing to build" surfaces as a Dagster failure instead of
# a valid skip.
type: retail_data_orchestration.components.dbt_state_reuse_patch.DbtStateReusePatchComponent
attributes: {}
YAML
```

```bash
cat > src/retail_data_orchestration/defs/scenario3_state_awareness/dbt_state_reuse_job.yaml <<'YAML'
# dbt Cloud job configured with `--select state:modified+` — dbt Cloud
# fetches the deferred state artifact automatically and only builds
# models whose SQL/config/upstream schema changed. Unchanged models
# return `status: no-op`, which the patch above translates into a
# Dagster materialization (asset stays green, task correctly skipped).
type: retail_data_orchestration.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario3_stateaware_job
  # In dbt Cloud, configure this job to use `--select state:modified+`
  # in its command; no need to specify select: here.
YAML
```

### 6b. Mechanism B — orchestrator-native freshness (no dbt Cloud call)

```bash
cat > src/retail_data_orchestration/defs/scenario3_state_awareness/native_freshness.yaml <<'YAML'
# 60-minute freshness SLA on a specific mart. If the last materialization
# is within 60 min, no need to run — Dagster's automation resolvers see
# the fresh state and skip. If it's older, the automation condition
# below trips and materialization runs.
type: retail_data_orchestration.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: snowflake_workspace/tables/MART/DAILY_SUMMARY
  maximum_lag_minutes: 60
YAML
```

Validate:

```bash
uv run dg check defs
```

---

## Step 7: Power BI refresh (S1.8 stretch)

`dagster-powerbi` is the OFFICIAL Dagster integration — you already installed it via `uv add`. Wire it into your project's root `definitions.py`:

```bash
cat > src/retail_data_orchestration/definitions.py <<'PY'
"""Definitions root — Dagster discovers assets via dg autoload, plus the
Power BI workspace defined manually below (official integration, not a
community component)."""
from dagster import Definitions, load_from_defs_folder
from dagster_powerbi import PowerBIServicePrincipal, PowerBIWorkspace
from pathlib import Path

# Autoload everything under src/retail_data_orchestration/defs/
_autoloaded_defs = load_from_defs_folder(
    project_root=Path(__file__).parent,
)

# Power BI workspace — refresh datasets/reports downstream of the mart.
_powerbi = PowerBIWorkspace(
    credentials=PowerBIServicePrincipal(
        tenant_id={"env": "POWERBI_TENANT_ID"},
        client_id={"env": "POWERBI_CLIENT_ID"},
        client_secret={"env": "POWERBI_CLIENT_SECRET"},
    ),
    workspace_id={"env": "POWERBI_WORKSPACE_ID"},
)

defs = Definitions.merge(
    _autoloaded_defs,
    _powerbi.build_defs(),
)
PY
```

**What this gets you.** `dagster-powerbi` discovers reports + datasets + semantic models in the workspace and emits assets for each. Assets that read from your Snowflake mart get a dependency edge back to the mart tables — one asset graph, source → mart → Power BI (OBS-07 + OBS-08 in one shot).

Validate:

```bash
uv run dg check defs
```

---

## Step 8: Retry policies for the "infra vs data" distinction (REC-06)

`RetryPolicy` sets the retry BUDGET — `max_retries`, `delay`, `backoff`, `jitter`. It does **not** have a built-in filter for "retry only certain exceptions." The infra-vs-data distinction lives in the compute function itself, using two Dagster primitives:

- `dagster.RetryRequested(...)` — raise this for a **transient / infrastructure** failure. Consumes one retry from the `RetryPolicy` budget and re-queues the step.
- `dagster.Failure(...)` (or any uncaught exception the caller doesn't wrap) — treated as a **data / permanent** failure. Does NOT consume a retry; the step is marked failed immediately and downstream is skipped.

Pattern for any component whose compute makes a network call — wrap it, map transient errors to `RetryRequested`, let everything else propagate:

```python
# Inside a component's compute function (illustration — real components
# already wrap their own API calls this way):
import dagster as dg
import requests

try:
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
except (requests.ConnectionError, requests.Timeout) as e:
    raise dg.RetryRequested(
        max_retries=3,
        seconds_to_wait=30,
    ) from e
except requests.HTTPError as e:
    if 500 <= e.response.status_code < 600:
        raise dg.RetryRequested(max_retries=3, seconds_to_wait=30) from e
    # 4xx = data/auth error — permanent, no retry
    raise dg.Failure(f"API returned {e.response.status_code}") from e
```

Add a `retry_policy:` block to any Dagster-native asset that needs a retry BUDGET (the specific behavior above is what actually filters what retries):

```yaml
# example: scenario 1 dbt_cloud_job.yaml augmented
type: retail_data_orchestration.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario1_job
  retry_policy:
    max_retries: 3
    delay: 60             # seconds
    backoff: exponential  # doubles between attempts
    # No `error_filter:` field exists on RetryPolicy. If the community
    # component's compute wraps its outbound API call as shown above,
    # `RetryRequested` uses this budget; a `Failure` bypasses it entirely.
```

**Why this matters for the POC.** REC-06 asks for retries that fire on infra failures but NOT on data failures. Vendors who show a slider labeled "retry on any exception" fail this criterion. Vendors who let the compute function classify the failure type — and then respect that classification against a policy budget — pass it. Dagster falls in the second camp.

---

## Step 9: Start Dagster + verify

```bash
uv run dg dev
# open http://localhost:3000
```

**What you should see** (after the first `snowflake_workspace` load — takes ~30 seconds):

- **Assets tab** — asset groups for `scenario1_snowpipe_dbt` (with every Snowpipe + table under the configured Snowflake schemas), `scenario2_replication_refresh` (HVR channels), `scenario3_state_awareness` (state-aware assets). Plus Power BI reports/datasets hanging off marts.
- **Sensors tab** — the two `dbt_cloud_job_sensor` sensors (one per scenario) + the workspace polling sensors.
- **Asset checks tab** — freshness checks on HVR + Snowflake mart tables.
- **Runs tab** — one shared runs history across all three scenarios.

`dg list defs` at the terminal gives you the exact asset keys — useful for the `triggering_assets:` field references in the sensor YAMLs. If a key doesn't match what you wrote, `dg check defs` catches it — fix the YAML, run `dg check` again.

---

## Step 10: Try the failure modes

The source doc's F1.1 through F2.6 grade specific behaviors. Trigger each on the real deployment:

| Failure | How to trigger | What to observe |
|---|---|---|
| F1.1 Python extract fails mid-run | Kill the extract job at ~50% completion | `snowflake_workspace` polling sensor stays quiet — no partial load, no downstream trigger. Dagster asset stays marked "materialization failed" until re-run. |
| F1.2 Malformed file → SnowPipe copy error | Land a corrupted file in S3 | The Snowpipe asset materialization surfaces `COPY_HISTORY.ERROR_MESSAGE` in its metadata blob. |
| F1.4 dbt test fails but model builds | Introduce a `not_null` violation in a downstream model | The dbt asset materializes green, but the associated `asset_check` fails red. Configure your automation/alerts on the check state, not the run state. |
| F1.5 dbt Cloud fails on one model mid-DAG | Introduce a syntax error in a specific model | Run "re-execute from failed" in the Dagster UI — the platform re-runs from the failed model without re-running upstream. |
| F1.6 Extract → zero files | Configure the extract to skip on a certain date | Snowpipe asset materializes but the polling sensor emits an `AssetObservation` with `rows_loaded=0`. Add a `freshness_check` or `asset_check` that fails when `rows_loaded == 0`. |
| F2.2 Refresh partial completion | Break one HVR target mid-refresh | `hvr_hub_workspace polling_sensor` per-table `AssetObservation` shows the partial state; the `freshness_lag_threshold_seconds` check fails only on the affected tables. |

---

## Step 11: Deploy to Dagster+

Local `dg dev` is your build environment. For the actual POC, ship to Dagster+ with the same YAMLs:

```bash
# One-time: install the plus CLI + authenticate
uv add dagster-cloud
uv run dagster-cloud config setup

# Deploy the current project to your target deployment
uv run dagster-cloud deployment add-location \
  --deployment prod \
  --location-name retail-data-orchestration-poc \
  --code-source-python-file src/retail_data_orchestration/definitions.py
```

Every subsequent code push triggers a build in Dagster+, respecting your branch-deployment rules for PR-level previews (DEV-03).

---

## Companion docs

- [retail_data_orchestration.md](retail_data_orchestration.md) — reference + criteria mapping table (every OBS-XX / TRG-XX / REC-XX / INT-XX / ERR-XX / ALT-XX / DEV-XX / OPS-XX mapped to specific components)
- [retail_data_orchestration_real_mode.md](retail_data_orchestration_real_mode.md) — swap guide (convert local demo → real POC, one YAML file at a time)
- [retail_data_orchestration_from_scratch.md](retail_data_orchestration_from_scratch.md) — local hands-on tutorial (uses stand-ins; no credentials required)
- [setup_retail_data_orchestration_demo.sh](setup_retail_data_orchestration_demo.sh) — local scaffold script

## Notes

- **Workspace over piecemeal.** `snowflake_workspace` covers 95% of Scenario 1's Snowflake surface with one YAML block. Reach for `snowflake_snowpipe_load_sensor` / `snowflake_snowpipe` / `external_snowflake_table` individually only when the workspace's defaults don't fit (per-pipe rate limiting, per-object custom `AssetSpec` attributes, etc.). Same pattern for HVR — `hvr_hub_workspace` is the primary; the piecemeal `hvr_channel_component` is for edge cases.
- **`polling_sensor: true` on the workspace replaces per-object sensors.** In scenario 1, the workspace polling sensor is what makes "trigger dbt on load completion" work — no separate `snowflake_snowpipe_load_sensor` needed unless you want per-file granularity.
- **`triggering_assets:` on sensors** takes exact asset keys. Run `dg list defs` after code-location load to see the workspace-generated keys, then paste them into the sensor YAMLs.
- **This tutorial's shell commands assume a bash/zsh terminal.** For PowerShell equivalents, replace `cat > file <<'YAML'` with `Set-Content file @'` blocks + trailing `'@`.
