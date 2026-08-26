#!/usr/bin/env bash
# Retail data orchestration demo — three scenarios in one Dagster project.
#
# WHAT THIS DEMONSTRATES (end-to-end, laptop-local, zero credentials)
#   Scenario 1: Python extract → object-store landing → load-completion
#               sensor → dbt build. Trigger is EVIDENCE of load completion
#               (files arrived) NOT a hardcoded delay.
#   Scenario 2: External replication API refresh → dbt build. Refresh is a
#               visible node with its own state; dbt only runs after
#               verified completion.
#   Scenario 3: State-awareness pattern — skip a task when its data is
#               already fresh. Two mechanisms: defer to dbt state, and
#               decide independently via FreshnessPolicy.
#
# STAND-INS (single-line YAML swaps get you to real POC — see
# POC_REAL_MODE.md scaffolded inside the project):
#   filesystem_monitor    → snowflake_snowpipe_load_sensor
#   dbt_project (Core)    → dbt_run_job + dbt_cloud_job_sensor
#   MinIO                 → AWS S3
#   DuckDB                → Snowflake
#   mock replication CLI  → hvr_hub_workspace (or fivetran_workspace)
#
# COMPONENTS INSTALLED AS FILES INSIDE THE PROJECT — not as a library
# import. Each `src/<pkg>/components/<id>/component.py` is copy-editable.
#
# COST: $0. Docker containers: MinIO only (~150MB image).
# TIME: ~3 min first run, ~1 min after MinIO image is cached.
#
# USAGE:
#   bash setup_retail_data_orchestration_demo.sh [project_dir]

set -eo pipefail

PROJECT_DIR="${1:-retail-data-orchestration}"
MINIO_PORT="${MINIO_PORT:-19000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-19001}"

# ── dependency check ────────────────────────────────────────────────────
for cmd in docker uv python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "✗ Missing required command: $cmd"
    case "$cmd" in
      docker) echo "  Install: https://docs.docker.com/get-docker/" ;;
      uv) echo "  Install: curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
    esac
    exit 1
  fi
done

# ── 1/7  fresh project scaffold ─────────────────────────────────────────
echo ">>> 1/7  Scaffolding Dagster project ($PROJECT_DIR)"
if [ -d "$PROJECT_DIR" ]; then
  echo "    ! $PROJECT_DIR already exists — removing"
  rm -rf "$PROJECT_DIR"
fi
uvx create-dagster project "$PROJECT_DIR" --uv-sync >/dev/null 2>&1
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG=$(python3 -c 'import os; print(os.path.basename(os.getcwd()).replace("-","_"))')

# All paths inside the project dir — memory rule: Windows portability.
mkdir -p data/incoming data/landing output extract_scripts \
         "src/$PKG/defs/scenario1_api_load_dbt" \
         "src/$PKG/defs/scenario2_replication_refresh" \
         "src/$PKG/defs/scenario3_state_awareness" \
         dbt_project/models/scenario1 dbt_project/models/scenario2 dbt_project/models/scenario3

# ── 2/7  install community components as files ──────────────────────────
echo ">>> 2/7  Installing community components as files (not library imports)"

CLI="uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git dagster-component"

# dbt_state_reuse_patch is a real-dbt-Cloud-only component (patches
# dagster-dbt's `no-op` handling). Local demo uses dbt Core which doesn't
# emit no-op — so we skip it here. See POC_REAL_MODE.md for its wiring.
for cid in \
  shell_command_asset \
  filesystem_monitor \
  freshness_check \
  event_automation \
; do
  echo "     • $cid"
  # `printf 'y\n'` answers the one interactive confirm without triggering
  # SIGPIPE (`yes` writes forever + `set -eo pipefail` catches SIGPIPE
  # exit=141). `--auto-install` skips the pip-install prompt; `--force`
  # makes the setup script idempotent (safe to re-run).
  printf 'y\n' | $CLI add "$cid" --auto-install --force >/dev/null 2>&1 || {
    echo "       ! add $cid failed — re-run interactively to see the error"
  }
done

# Add the official Power BI integration for the S1.8 stretch. Real POC uses
# this; local demo has it in requirements but doesn't wire an active
# PowerBIWorkspace (would need real credentials).
echo "     • dagster-powerbi (official Dagster integration)"
uv add dagster-powerbi >/dev/null 2>&1 || echo "       ! dagster-powerbi install skipped"

# Clean up any auto-installed sample defs from `dagster-component add
# --auto-install` — we write our own below with distinct directory names.
find "src/$PKG/defs" -maxdepth 2 -name defs.yaml -not -path "*/scenario1_*" \
  -not -path "*/scenario2_*" -not -path "*/scenario3_*" -delete 2>/dev/null || true

# ── 3/7  MinIO docker-compose (S3-compatible landing zone) ──────────────
echo ">>> 3/7  Writing docker-compose.yml (MinIO)"

cat > docker-compose.yml <<COMPOSEEOF
name: retail-data-orchestration

services:
  minio:
    image: minio/minio:latest
    container_name: rdo-minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "${MINIO_PORT}:9000"
      - "${MINIO_CONSOLE_PORT}:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 20
COMPOSEEOF

# ── 4/7  extract scripts (stand-in for the "existing Python job") ───────
echo ">>> 4/7  Writing extract scripts"

cat > extract_scripts/api_extract.py <<'PYEOF'
#!/usr/bin/env python3
"""Stand-in for an existing production Python extract.

Writes an unpredictable number of gz-JSON files to the landing directory,
one per (endpoint, site) pair. Real deployments would call the real API
and push to S3 instead of a local directory. The `shell_command_asset`
YAML that invokes this file is unchanged either way — G7 (existing job
runs unchanged) satisfied.
"""
import argparse
import gzip
import json
import random
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    endpoints = ["site_details", "site", "transactions"]
    sites = ["site_001", "site_002", "site_003"]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for endpoint in endpoints:
        for site in sites:
            path = out / f"{endpoint}__{site}__{ts}.json.gz"
            payload = {
                "endpoint": endpoint,
                "site_id": site,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "rows": [
                    {"kwh": round(random.uniform(1.0, 45.0), 2),
                     "session_seconds": random.randint(60, 3600)}
                    for _ in range(random.randint(5, 25))
                ],
            }
            with gzip.open(path, "wt") as f:
                json.dump(payload, f)
    print(f"wrote {len(endpoints) * len(sites)} files to {out}")


if __name__ == "__main__":
    main()
PYEOF
chmod +x extract_scripts/api_extract.py

cat > extract_scripts/mock_replication_refresh.py <<'PYEOF'
#!/usr/bin/env python3
"""Stand-in for `hvr_hub_workspace action:refresh`.

Simulates: POST /channels/fuel_price02/refresh → poll → completion.
Writes a small CSV to `data/landing/fuel_prices.csv` and prints the row
count so the shell_command_asset can surface it as materialization
metadata."""
import csv
import random
from pathlib import Path


def main() -> None:
    dest = Path("data/landing/fuel_prices.csv")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = random.randint(5_000, 5_500)
    with dest.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site_id", "product", "price_cents", "captured_at"])
        for i in range(n):
            w.writerow([
                f"site_{i % 500:04d}",
                random.choice(["diesel", "def", "regular", "premium"]),
                random.randint(280, 480),
                "2026-08-26T09:00:00Z",
            ])
    print(f"rows_replicated={n}")


if __name__ == "__main__":
    main()
PYEOF
chmod +x extract_scripts/mock_replication_refresh.py

# ── 5/7  dbt Core project (dbt Cloud stand-in) ──────────────────────────
echo ">>> 5/7  Writing local dbt project (Core against DuckDB — stand-in for dbt Cloud)"

cat > dbt_project/dbt_project.yml <<'DBTEOF'
name: retail_dbt
version: '1.0.0'
config-version: 2
profile: retail
model-paths: [models]
target-path: target
clean-targets: [target, dbt_packages]
DBTEOF

cat > dbt_project/profiles.yml <<'PROFEOF'
retail:
  target: local
  outputs:
    local:
      type: duckdb
      path: ../data/landing/warehouse.duckdb
      threads: 4
PROFEOF

# Scenario 1 model
cat > dbt_project/models/scenario1/stg_api_events.sql <<'SQLEOF'
{{ config(materialized='table', tags=['scenario1']) }}
-- Stand-in for the real dbt Cloud scenario-1 staging model. In the real
-- POC this is fuel_and_trading.stg_<endpoint>.
select
  'site_001'::VARCHAR as site_id,
  now() as loaded_at,
  1 as sample_row
SQLEOF

cat > dbt_project/models/scenario1/mart_daily_summary.sql <<'SQLEOF'
{{ config(materialized='table', tags=['scenario1']) }}
select
  current_date as day,
  count(*) as row_count
from {{ ref('stg_api_events') }}
SQLEOF

# Scenario 2 model
cat > dbt_project/models/scenario2/stg_fuel_prices.sql <<'SQLEOF'
{{ config(materialized='table', tags=['scenario2']) }}
-- Real POC: fuel_and_trading.stg_fuel_prices reading FUEL_PRICES.LANDING
select 1 as sample_row
SQLEOF

# Scenario 3 model
cat > dbt_project/models/scenario3/mart_stateaware.sql <<'SQLEOF'
{{ config(materialized='table', tags=['scenario3']) }}
select 1 as sample_row
SQLEOF

# ── 6/7  defs.yaml files — 3 scenarios ──────────────────────────────────
echo ">>> 6/7  Writing defs.yaml for all three scenarios"

# ── Scenario 1 ──────────────────────────────────────────────────────────
cat > "src/$PKG/defs/scenario1_api_load_dbt/extract.yaml" <<YAMLEOF
# Scenario 1 — Python extract (G7: existing pipeline unchanged).
# Real POC: same YAML — command points at your real extract script.
type: $PKG.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: raw_api_files
  command: "python3 $PROJECT_ABS/extract_scripts/api_extract.py --out $PROJECT_ABS/data/incoming"
  group_name: scenario1_api_load_dbt
  kinds: [python, api]
YAMLEOF

cat > "src/$PKG/defs/scenario1_api_load_dbt/load_completion_sensor.yaml" <<YAMLEOF
# LOCAL: filesystem_monitor watches the landing dir.
# REAL:  swap the type: below to snowflake_snowpipe_load_sensor
#        (see POC_REAL_MODE.md for the field mapping).
type: $PKG.components.filesystem_monitor.FilesystemMonitorSensorComponent
attributes:
  sensor_name: api_load_complete_sensor
  directory_path: $PROJECT_ABS/data/incoming
  file_pattern: ".*\\\\.json\\\\.gz$"
  job_name: dbt_build_scenario1_job
  minimum_interval_seconds: 30
  recursive: false
  default_status: stopped
YAMLEOF

cat > "src/$PKG/defs/scenario1_api_load_dbt/dbt.yaml" <<YAMLEOF
# LOCAL: shell_command_asset invokes dbt Core against DuckDB. The point of
# this scenario is triggering dbt on evidence of load completion, not the
# dbt asset modeling — so a shell-out is sufficient for the LOCAL story.
# REAL:  swap type: to dbt_run_job (kicks off dbt Cloud) + add a sibling
#        dbt_cloud_job_sensor.yaml. Real dbt Cloud emits per-model
#        asset-check pass/fail via dbt_cloud_job_sensor.
type: $PKG.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: dbt_build_scenario1
  command: "cd $PROJECT_ABS/dbt_project && dbt build --select tag:scenario1 --profiles-dir ."
  group_name: scenario1_api_load_dbt
  kinds: [dbt]
  deps: [raw_api_files]
YAMLEOF

# ── Scenario 2 ──────────────────────────────────────────────────────────
cat > "src/$PKG/defs/scenario2_replication_refresh/refresh.yaml" <<YAMLEOF
# LOCAL: shell_command_asset invokes a mock replication script.
# REAL:  hvr_hub_workspace with action:refresh + wait_for_completion.
type: $PKG.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: replicated_landing_tables
  command: "python3 $PROJECT_ABS/extract_scripts/mock_replication_refresh.py"
  group_name: scenario2_replication_refresh
  kinds: [replication]
YAMLEOF

cat > "src/$PKG/defs/scenario2_replication_refresh/dbt.yaml" <<YAMLEOF
# Same shell_command_asset shim as scenario 1. Real POC uses dbt_run_job
# + dbt_cloud_job_sensor pointed at your Gemini Enterprise dbt Cloud job.
type: $PKG.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: dbt_build_scenario2
  command: "cd $PROJECT_ABS/dbt_project && dbt build --select tag:scenario2 --profiles-dir ."
  group_name: scenario2_replication_refresh
  kinds: [dbt]
  deps: [replicated_landing_tables]
YAMLEOF

cat > "src/$PKG/defs/scenario2_replication_refresh/freshness.yaml" <<YAMLEOF
# 15-minute SLA on the replicated landing tables — becomes a Dagster asset
# check. When the last materialization is > 15 min old, the check fails
# and can gate downstream / alert on it.
type: $PKG.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: replicated_landing_tables
  maximum_lag_minutes: 15
YAMLEOF

# ── Scenario 3 ──────────────────────────────────────────────────────────
cat > "src/$PKG/defs/scenario3_state_awareness/native_freshness.yaml" <<YAMLEOF
# Mechanism B — orchestrator-native freshness. FreshnessPolicy on the
# mart. Dagster decides skip-or-run against its own clock; no dbt Cloud
# call anywhere in the decision path.
type: $PKG.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: mart_daily_summary
  maximum_lag_minutes: 60
YAMLEOF

cat > "src/$PKG/defs/scenario3_state_awareness/automation_conditional.yaml" <<YAMLEOF
# Automation condition: run when any parent asset newly materialized
# (eager), UNLESS this asset itself was materialized within the last hour
# (skip when already fresh).
type: $PKG.components.event_automation.EventAutomationComponent
attributes:
  asset_key: mart_stateaware
  automation_condition: "{{ dg.AutomationCondition.eager() & ~dg.AutomationCondition.newly_materialized_within(hours=1) }}"
YAMLEOF

# ── 7/7  README + POC_REAL_MODE.md ──────────────────────────────────────
echo ">>> 7/7  Writing README + POC_REAL_MODE.md"

cat > README.md <<'READMEEOF'
# Retail Data Orchestration — three-scenario demo

Runnable on a laptop with zero credentials. See
[retail_data_orchestration.md](https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/retail_data_orchestration.md)
for the full walkthrough + criteria mapping table.

## Start the demo

```bash
docker compose up -d
uv run dg dev
# open http://localhost:3000
```

Three asset groups appear:
- `scenario1_api_load_dbt` — API extract → load sensor → dbt build
- `scenario2_replication_refresh` — external refresh → dbt build
- `scenario3_state_awareness` — freshness-driven skip patterns

### Try scenario 1 end-to-end

1. Enable the `api_load_complete_sensor` (Sensors tab → toggle on).
2. Materialize `raw_api_files`. The Python extract writes 9 files.
3. Within 30 seconds the sensor fires and kicks off the dbt build.
4. Watch the sensor tick log surface which files caused the trigger.

### Switch to real POC mode

See [POC_REAL_MODE.md](./POC_REAL_MODE.md) — every stand-in is a single YAML
line swap. Estimated conversion: 15 min per scenario once credentials are in
place.
READMEEOF

cat > POC_REAL_MODE.md <<'REALEOF'
# Real POC mode — swap guide

Each stand-in below maps to a single-file YAML replacement. Env vars for
credentials go into a project-root `.env` file that Dagster loads
automatically.

## Scenario 1

### 1a. Load-completion sensor: `filesystem_monitor` → `snowflake_snowpipe_load_sensor`

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

Run: `dagster-component add snowflake_snowpipe_load_sensor` first — copies
the sensor component's files into your project.

### 1b. Python extract path

Same `shell_command_asset` component; only the `command:` changes. Point at
your real script's install path and switch its output flag from
`--out /workspace/data/incoming` to your real S3 URI.

### 1c. dbt: `dbt_project` (Core) → `dbt_run_job` + `dbt_cloud_job_sensor`

Replace `src/<pkg>/defs/scenario1_api_load_dbt/dbt.yaml`:

```yaml
type: <pkg>.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario1_job
  # This is a Dagster job; it doesn't need a schedule — the SnowPipe
  # sensor above triggers it. But you can add a scheduled sibling for
  # calendar-driven runs too.
```

Add sibling `dbt_cloud_job_sensor.yaml`:

```yaml
type: <pkg>.components.dbt_cloud_job_sensor.DbtCloudJobSensorComponent
attributes:
  sensor_name: dbt_cloud_status_sensor
  dbt_cloud_job_id: "{{ env('DBT_CLOUD_JOB_ID_SCENARIO1') }}"
  dbt_cloud_api_token: "{{ env('DBT_CLOUD_API_TOKEN') }}"
  dbt_cloud_account_id: "{{ env('DBT_CLOUD_ACCOUNT_ID') }}"
  minimum_interval_seconds: 60
```

Run: `dagster-component add dbt_run_job dbt_cloud_job_sensor` first.

### 1d. External Snowflake tables (OBS-02)

Add per mart table:

```yaml
type: <pkg>.components.external_snowflake_table.ExternalSnowflakeTableComponent
attributes:
  database: FUEL_AND_TRADING
  schema:   MART
  table:    daily_summary
```

### 1e. Power BI refresh (S1.8 stretch)

Use the OFFICIAL `dagster-powerbi` integration (not a community component).
Add to your `definitions.py` root:

```python
from dagster_powerbi import PowerBIWorkspace, PowerBIToken

powerbi = PowerBIWorkspace(
    credentials=PowerBIToken(api_token="{{ env('POWERBI_TOKEN') }}"),
    workspace_id="{{ env('POWERBI_WORKSPACE_ID') }}",
)
# Then include powerbi.build_defs() in your Definitions.
```

`dagster-powerbi` emits report / dataset / semantic model assets that hang
off your mart tables — one asset graph, source → mart → Power BI.

## Scenario 2

### 2a. Refresh: `shell_command_asset` → `hvr_hub_workspace`

Replace `src/<pkg>/defs/scenario2_replication_refresh/refresh.yaml`:

```yaml
type: <pkg>.components.hvr_hub_workspace.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"
    hub_name: "{{ env.HVR_HUB_NAME }}"
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
  channel_selector:
    by_pattern: ["fuel_price*"]
  action: refresh
  wait_for_completion: true
  poll_interval_seconds: 30
  timeout_seconds: 1800
  polling_sensor: true
  observation_interval_seconds: 300
  freshness_lag_threshold_seconds: 900
```

Run: `dagster-component add hvr_hub_workspace` first.

### 2b. dbt: same swap as 1c.

## Scenario 3

No swaps needed for the local demo — `dbt_state_reuse_patch`,
`freshness_check`, and `event_automation` all work against dbt Core / DuckDB
identically to real dbt Cloud / Snowflake. The one real-mode addition:

```yaml
# scenario3_state_awareness/dbt_build_selective.yaml — REAL mode.
type: <pkg>.components.dbt_run_job.DbtRunJobComponent
attributes:
  job_name: dbt_build_scenario3_stateaware
  command: build
  select: "state:modified+"
  # In dbt Cloud, deferred-state artifacts are fetched automatically.
```

Run: `dagster-component add dbt_run_job` first.

## Env vars checklist

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
REALEOF

# ── final summary ───────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════════"
echo "  Retail Data Orchestration demo scaffolded at:"
echo "    $PROJECT_ABS"
echo
echo "  Next steps:"
echo "    cd $PROJECT_DIR"
echo "    docker compose up -d              # start MinIO"
echo "    uv run dg dev                     # http://localhost:3000"
echo
echo "  See:"
echo "    ./README.md            — quickstart"
echo "    ./POC_REAL_MODE.md     — real Snowflake / dbt Cloud / HVR swap"
echo "════════════════════════════════════════════════════════════════════"
