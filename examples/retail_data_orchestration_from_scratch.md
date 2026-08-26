# Retail Data Orchestration — from-scratch tutorial

Hands-on companion to [retail_data_orchestration.md](retail_data_orchestration.md). Start from an empty terminal, type every command, paste every file, understand what each piece does. Ends in the exact same working demo the [scaffold script](setup_retail_data_orchestration_demo.sh) produces — you just built it yourself.

> **When to use which doc.**
> - **[Scaffold script](setup_retail_data_orchestration_demo.sh)** — you want it working in ~3 minutes to inspect the output.
> - **This doc (from-scratch)** — you want to understand each moving piece, so you can build a similar demo for a different scenario later. Estimated time: ~30 minutes.
> - **[Real-mode swap guide](retail_data_orchestration_real_mode.md)** — you already have it working locally and want to convert to real Snowflake / dbt Cloud / HVR.

---

## Prerequisites

Install these once, if you haven't already:

```bash
# uv — Python project + venv manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Docker Desktop (for the MinIO container)
# https://docs.docker.com/get-docker/

# Verify
uv --version
docker --version
python3 --version   # 3.10 or newer
```

That's it. No Snowflake account, no dbt Cloud account, no HVR license — everything runs on your laptop.

---

## Step 1: Bootstrap the Dagster project

`uvx create-dagster` scaffolds a canonical Dagster project — same layout every `dg` command expects.

```bash
uvx create-dagster project retail-data-orchestration --uv-sync
cd retail-data-orchestration
```

The `--uv-sync` flag creates a `.venv/` and installs Dagster + `dg` (the Dagster CLI wrapper). No global installs.

**What you got:**

```
retail-data-orchestration/
├── pyproject.toml            — project metadata + deps
├── uv.lock                    — reproducible dependency pins
├── dg.toml                    — dg CLI config (points at the src/ package)
├── .venv/                     — uv-managed virtualenv
└── src/
    └── retail_data_orchestration/
        ├── __init__.py
        ├── definitions.py     — Dagster root; discovers everything under defs/
        └── defs/              — one dir per component instance, `dg` auto-loads
            └── .gitkeep
```

The `defs/` autoload is the magic. Every `defs.yaml` (and every scaffolded component dir) under `src/<pkg>/defs/` gets loaded automatically — no manual `Definitions([...])` list to maintain.

Verify it's alive:

```bash
uv run dg check defs
```

Expected output:

```
All component YAML validated successfully.
All definitions loaded successfully.
```

*(You'll see this after every step — it's the cheapest "does the project still parse" check.)*

---

## Step 2: Install community components as files inside your project

The `dagster-component-cli` CLI has an `add` command that copies a community component's `.py` + `schema.json` + `README.md` directly into your project. **Not** a library import — actual files you can read and edit.

Set up a shortcut for the rest of the tutorial:

```bash
alias dcc="uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git dagster-component"
```

Install the four components the local demo needs. Answer `y` to the "Continue?" prompt for each; add `--auto-install --force` to skip prompts entirely.

```bash
dcc add shell_command_asset  --auto-install
dcc add filesystem_monitor   --auto-install
dcc add freshness_check      --auto-install
dcc add event_automation     --auto-install
```

**Inspect what landed** — this is the whole point of the file-install pattern:

```bash
ls src/retail_data_orchestration/components/shell_command_asset/
# README.md    __init__.py    component.py    requirements.txt    schema.json
```

Every component's source code lives in your project. `cat` the `component.py` — that's the Dagster implementation. Modify it locally if a customer needs to.

**Also inspect what's under `defs/`:**

```bash
ls src/retail_data_orchestration/defs/
# Each `dcc add` also drops a placeholder `<component_id>/defs.yaml`
# with fields as `<fill in>`. We'll delete these and write our own
# under distinct scenario-specific directory names.
```

Clean up the placeholders — we're writing scenario-organized YAML below:

```bash
rm -rf src/retail_data_orchestration/defs/shell_command_asset \
       src/retail_data_orchestration/defs/filesystem_monitor \
       src/retail_data_orchestration/defs/freshness_check \
       src/retail_data_orchestration/defs/event_automation
```

**Why these four components:**

| Component | Role in the demo |
|---|---|
| `shell_command_asset` | Runs the "existing Python job" unchanged, as a Dagster asset. Also acts as a stand-in for `dbt_run_job` and `hvr_hub_workspace` in the local demo (they need real credentials). |
| `filesystem_monitor` | Fires a Dagster job when files arrive in a directory. Stand-in for `snowflake_snowpipe_load_sensor` — same job-triggering pattern, no Snowflake needed. |
| `freshness_check` | Attaches a `FreshnessPolicy` asset check. When the last materialization is older than the SLA, the check fails — surfaces staleness as a first-class signal. |
| `event_automation` | Wraps Dagster's `AutomationCondition` in YAML. Used for scenario 3's skip-when-fresh pattern. |

---

## Step 3: Add the official Power BI integration (optional stretch)

For scenario 1's stretch goal (S1.8 — Power BI refresh on completion), Dagster's official `dagster-powerbi` package is the right primitive — NOT a community component. Add it now if you want the stretch, or skip and come back to it later:

```bash
uv add dagster-powerbi
```

We won't wire it in the local demo (would need real Power BI credentials), but it's ready for when you convert to real POC mode.

---

## Step 4: Local infrastructure — one Docker container

MinIO gives you an S3-compatible object store for the "land files, then trigger downstream" pattern. Create `docker-compose.yml` at the project root:

```bash
cat > docker-compose.yml <<'YAML'
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
      - "19000:9000"
      - "19001:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 20
YAML
```

Start it:

```bash
docker compose up -d
```

**Verify** — MinIO's admin console is now at http://localhost:19001 (login `minioadmin` / `minioadmin`). This is your local S3.

---

## Step 5: Set up the local dbt project (Core, targets DuckDB)

Real POC runs against dbt Cloud + Snowflake. Local demo swaps in dbt Core against DuckDB — same SQL, same test framework, same asset-check model. `POC_REAL_MODE.md` documents the swap.

```bash
mkdir -p dbt_project/models/scenario1 dbt_project/models/scenario2 dbt_project/models/scenario3
mkdir -p data/landing data/incoming
```

**`dbt_project/dbt_project.yml`:**

```bash
cat > dbt_project/dbt_project.yml <<'YAML'
name: retail_dbt
version: '1.0.0'
config-version: 2
profile: retail
model-paths: [models]
target-path: target
clean-targets: [target, dbt_packages]
YAML
```

**`dbt_project/profiles.yml`** — points at a DuckDB file in `data/landing/`:

```bash
cat > dbt_project/profiles.yml <<'YAML'
retail:
  target: local
  outputs:
    local:
      type: duckdb
      path: ../data/landing/warehouse.duckdb
      threads: 4
YAML
```

**Three tiny models** — one per scenario, each tagged so dbt's `--select tag:scenarioN` picks the right one:

```bash
cat > dbt_project/models/scenario1/stg_api_events.sql <<'SQL'
{{ config(materialized='table', tags=['scenario1']) }}
select
  'site_001'::VARCHAR as site_id,
  now() as loaded_at,
  1 as sample_row
SQL

cat > dbt_project/models/scenario1/mart_daily_summary.sql <<'SQL'
{{ config(materialized='table', tags=['scenario1']) }}
select current_date as day, count(*) as row_count from {{ ref('stg_api_events') }}
SQL

cat > dbt_project/models/scenario2/stg_fuel_prices.sql <<'SQL'
{{ config(materialized='table', tags=['scenario2']) }}
select 1 as sample_row
SQL

cat > dbt_project/models/scenario3/mart_stateaware.sql <<'SQL'
{{ config(materialized='table', tags=['scenario3']) }}
select 1 as sample_row
SQL
```

Install the two Python packages dbt Core + DuckDB need:

```bash
uv add dbt-core dbt-duckdb
```

Smoke-test dbt runs locally:

```bash
cd dbt_project
uv run dbt build --profiles-dir . --select tag:scenario1
cd ..
```

You should see two models compiled + built. The DuckDB file at `data/landing/warehouse.duckdb` now has two tables.

---

## Step 6: Extract scripts — stand-ins for the "existing production Python jobs"

Two small Python scripts stand in for the real extract and the real replication call. `shell_command_asset` will invoke them from Dagster.

```bash
mkdir -p extract_scripts
```

**`extract_scripts/api_extract.py`** — writes N gz-JSON files to the landing directory, one per (endpoint, site). Real deployments would call a real API and push to real S3. The Dagster YAML that invokes it is unchanged either way — that's G7 satisfied.

```bash
cat > extract_scripts/api_extract.py <<'PY'
#!/usr/bin/env python3
"""Stand-in for an existing production Python extract."""
import argparse, gzip, json, random
from datetime import datetime, timezone
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    endpoints = ["site_details", "site", "transactions"]
    sites = ["site_001", "site_002", "site_003"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    for endpoint in endpoints:
        for site in sites:
            path = out / f"{endpoint}__{site}__{ts}.json.gz"
            payload = {
                "endpoint": endpoint, "site_id": site,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "rows": [
                    {"kwh": round(random.uniform(1.0, 45.0), 2),
                     "session_seconds": random.randint(60, 3600)}
                    for _ in range(random.randint(5, 25))
                ],
            }
            with gzip.open(path, "wt") as f: json.dump(payload, f)
    print(f"wrote {len(endpoints) * len(sites)} files to {out}")

if __name__ == "__main__": main()
PY
chmod +x extract_scripts/api_extract.py
```

**`extract_scripts/mock_replication_refresh.py`** — simulates HVR's `hvrrefresh` command. Writes a small CSV, prints the row count so Dagster can surface it as materialization metadata.

```bash
cat > extract_scripts/mock_replication_refresh.py <<'PY'
#!/usr/bin/env python3
"""Stand-in for `hvr_hub_workspace action:refresh`."""
import csv, random
from pathlib import Path

def main():
    dest = Path("data/landing/fuel_prices.csv")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = random.randint(5_000, 5_500)
    with dest.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site_id", "product", "price_cents", "captured_at"])
        for i in range(n):
            w.writerow([f"site_{i % 500:04d}",
                        random.choice(["diesel", "def", "regular", "premium"]),
                        random.randint(280, 480), "2026-08-26T09:00:00Z"])
    print(f"rows_replicated={n}")

if __name__ == "__main__": main()
PY
chmod +x extract_scripts/mock_replication_refresh.py
```

**Test them** — each writes files to `data/`:

```bash
python3 extract_scripts/api_extract.py --out ./data/incoming
ls data/incoming/
# should show 9 .json.gz files

python3 extract_scripts/mock_replication_refresh.py
head -3 data/landing/fuel_prices.csv
```

Clean them up before wiring Dagster (Dagster will re-run them via `shell_command_asset`):

```bash
rm -rf data/incoming/*
```

---

## Step 7: Scenario 1 defs — API extract → load sensor → dbt build

Three YAML files under `src/retail_data_orchestration/defs/scenario1_api_load_dbt/`. Let's set an absolute-path env var to keep the YAML portable — Dagster's YAML doesn't do relative-path resolution the way you might expect.

```bash
export PROJECT_ABS="$(pwd)"
mkdir -p src/retail_data_orchestration/defs/scenario1_api_load_dbt
```

**`extract.yaml`** — the Python extract as a Dagster asset. `shell_command_asset` runs the command, captures stdout/stderr into asset metadata, and marks the asset materialized on exit-0.

```bash
cat > src/retail_data_orchestration/defs/scenario1_api_load_dbt/extract.yaml <<YAML
# Scenario 1 — Python extract (G7: reuse existing pipeline unchanged).
# Real POC: same YAML — command points at your real extract's install path.
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: raw_api_files
  command: "python3 $PROJECT_ABS/extract_scripts/api_extract.py --out $PROJECT_ABS/data/incoming"
  group_name: scenario1_api_load_dbt
  kinds: [python, api]
YAML
```

**Walkthrough of each field:**
- `type:` — the fully-qualified component class. Because we installed the component as files inside the project, the path is `<pkg>.components.<id>.<Class>`.
- `asset_name:` — the asset key in Dagster's graph. Downstream YAMLs reference this via `deps: [raw_api_files]`.
- `command:` — the exact shell string to run. `$PROJECT_ABS` is substituted at YAML-load time (because we used unquoted heredoc); the file that lands has the absolute path baked in.
- `group_name:` — Dagster UI groups. All scenario-1 assets sit together.
- `kinds:` — badges in the catalog. Rendered as icons.

**`load_completion_sensor.yaml`** — the "trigger on evidence of load completion" primitive.

```bash
cat > src/retail_data_orchestration/defs/scenario1_api_load_dbt/load_completion_sensor.yaml <<YAML
# LOCAL: filesystem_monitor watches the landing dir. Fires when files match
# the glob.
# REAL:  swap type: to snowflake_snowpipe_load_sensor. Same field shape
# (sensor_name, job_name, minimum_interval_seconds) — the sensor now reads
# COPY_HISTORY instead of the local filesystem.
type: retail_data_orchestration.components.filesystem_monitor.FilesystemMonitorSensorComponent
attributes:
  sensor_name: api_load_complete_sensor
  directory_path: $PROJECT_ABS/data/incoming
  file_pattern: ".*\\\\.json\\\\.gz$"
  job_name: dbt_build_scenario1_job
  minimum_interval_seconds: 30
  recursive: false
  default_status: stopped         # start stopped so we can enable in the UI when we're ready
YAML
```

**Key point about this file:** the sensor references `dbt_build_scenario1_job` — a job that doesn't exist yet. That's fine. Dagster resolves job references at load time, and the job is defined by the dbt-invocation asset in the next YAML.

Actually — because the local demo uses `shell_command_asset` to invoke dbt (rather than dbt_run_job which explicitly declares a job), we need to reference the ASSET, not a job. Let me handle this by making the sensor auto-materialize the downstream asset instead. Update the sensor:

```bash
cat > src/retail_data_orchestration/defs/scenario1_api_load_dbt/load_completion_sensor.yaml <<YAML
# LOCAL: filesystem_monitor triggers materialization of the dbt asset when
# files land in the incoming directory.
type: retail_data_orchestration.components.filesystem_monitor.FilesystemMonitorSensorComponent
attributes:
  sensor_name: api_load_complete_sensor
  directory_path: $PROJECT_ABS/data/incoming
  file_pattern: ".*\\\\.json\\\\.gz$"
  # Note: filesystem_monitor targets a JOB by name. In this demo, the dbt
  # invocation is an asset, not an explicit job. Dagster synthesizes an
  # implicit "asset job" — reference it here by naming a job the same as
  # the target asset with `_job` suffix.
  job_name: __ASSET_JOB
  minimum_interval_seconds: 30
  recursive: false
  default_status: stopped
YAML
```

**`dbt.yaml`** — invoke dbt Core via shell.

```bash
cat > src/retail_data_orchestration/defs/scenario1_api_load_dbt/dbt.yaml <<YAML
# LOCAL: shell_command_asset invokes dbt Core against DuckDB. The point of
# scenario 1 is triggering dbt on evidence of load completion — the dbt
# asset modeling itself isn't the interesting part locally.
# REAL:  swap type: to dbt_run_job (kicks off dbt Cloud at selector
# granularity) + add a sibling dbt_cloud_job_sensor.yaml.
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: dbt_build_scenario1
  command: "cd $PROJECT_ABS/dbt_project && uv run dbt build --select tag:scenario1 --profiles-dir ."
  group_name: scenario1_api_load_dbt
  kinds: [dbt]
  deps: [raw_api_files]
YAML
```

**Validate what you've built so far:**

```bash
uv run dg check defs
```

If you see errors, they're usually one of:
- Missing `deps:` reference (typo in an asset name)
- Missing `type:` path (wrong module path — check `src/retail_data_orchestration/components/<id>/__init__.py`)
- YAML indentation (2 spaces, always)

---

## Step 8: Scenario 2 defs — external refresh → dbt build

```bash
mkdir -p src/retail_data_orchestration/defs/scenario2_replication_refresh
```

**`refresh.yaml`** — the mock refresh script standing in for HVR.

```bash
cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/refresh.yaml <<YAML
# LOCAL: shell_command_asset invokes the mock refresh script.
# REAL:  swap type: to hvr_hub_workspace with action:refresh +
# wait_for_completion. That component POSTs /channels/{c}/refresh and
# polls until complete, then materializes assets per (channel × target ×
# table).
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: replicated_landing_tables
  command: "cd $PROJECT_ABS && python3 extract_scripts/mock_replication_refresh.py"
  group_name: scenario2_replication_refresh
  kinds: [replication]
YAML
```

**`dbt.yaml`** — same pattern as scenario 1.

```bash
cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/dbt.yaml <<YAML
type: retail_data_orchestration.components.shell_command_asset.ShellCommandAssetComponent
attributes:
  asset_name: dbt_build_scenario2
  command: "cd $PROJECT_ABS/dbt_project && uv run dbt build --select tag:scenario2 --profiles-dir ."
  group_name: scenario2_replication_refresh
  kinds: [dbt]
  deps: [replicated_landing_tables]
YAML
```

**`freshness.yaml`** — 15-min SLA on the landing tables. Becomes a Dagster asset check.

```bash
cat > src/retail_data_orchestration/defs/scenario2_replication_refresh/freshness.yaml <<YAML
type: retail_data_orchestration.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: replicated_landing_tables
  maximum_lag_minutes: 15
YAML
```

**What the freshness_check gets you:**
- A first-class asset check on the target asset — pass/fail visible in the Dagster UI.
- Failing checks can gate downstream materializations OR trigger alerts.
- Different from a run failure: a freshness failure means "the last run succeeded but was too long ago," which is a different remediation than "a run errored."

Validate:

```bash
uv run dg check defs
```

---

## Step 9: Scenario 3 defs — state-aware skip

```bash
mkdir -p src/retail_data_orchestration/defs/scenario3_state_awareness
```

**`native_freshness.yaml`** — Mechanism B: the orchestrator evaluates staleness against its own clock. No dbt Cloud call in the decision path.

```bash
cat > src/retail_data_orchestration/defs/scenario3_state_awareness/native_freshness.yaml <<YAML
# Mechanism B — orchestrator-native freshness. FreshnessPolicy on the mart.
# Dagster decides skip-or-run against its own clock; no dbt Cloud call
# anywhere in the decision path.
type: retail_data_orchestration.components.freshness_check.FreshnessCheckComponent
attributes:
  asset_key: mart_daily_summary
  maximum_lag_minutes: 60
YAML
```

**`automation_conditional.yaml`** — Dagster's declarative automation: run when parents newly materialized, UNLESS this asset was itself materialized within the last hour.

```bash
cat > src/retail_data_orchestration/defs/scenario3_state_awareness/automation_conditional.yaml <<YAML
type: retail_data_orchestration.components.event_automation.EventAutomationComponent
attributes:
  asset_key: mart_stateaware
  automation_condition: "{{ dg.AutomationCondition.eager() & ~dg.AutomationCondition.newly_materialized_within(hours=1) }}"
YAML
```

**What this expresses:**
- `dg.AutomationCondition.eager()` — a parent materialized → this asset should run
- `~dg.AutomationCondition.newly_materialized_within(hours=1)` — UNLESS this asset was itself materialized in the last hour (i.e., it's still fresh)
- The `&` combines them: run when a parent materialized AND we're not still fresh.

**Mechanism A** (defer to dbt state — `no-op` responses treated as materialization) requires the `dbt_state_reuse_patch` community component + real dbt Cloud. It's documented in the [real-mode swap guide](retail_data_orchestration_real_mode.md#scenario-3--state-aware-skip). Not included in the local demo because local dbt Core doesn't emit `no-op` responses.

Validate:

```bash
uv run dg check defs
```

Expected:

```
All component YAML validated successfully.
All definitions loaded successfully.
```

---

## Step 10: Run the Dagster UI

```bash
uv run dg dev
# open http://localhost:3000
```

**What you should see:**

- **Assets tab** — three groups: `scenario1_api_load_dbt`, `scenario2_replication_refresh`, `scenario3_state_awareness`. Each with the assets we defined.
- **Sensors tab** — `api_load_complete_sensor` (stopped, since we set `default_status: stopped`).
- **Asset checks tab** — freshness checks on `replicated_landing_tables` and `mart_daily_summary`.

---

## Step 11: Run scenario 1 end-to-end

The interesting demo — trigger dbt on evidence of load completion, not on a hardcoded delay.

1. In the UI, go to **Sensors** → toggle `api_load_complete_sensor` to **ON**.
2. Go to **Assets** → select `raw_api_files` → **Materialize**.
3. Watch the Python extract run. It writes 9 gz-JSON files to `data/incoming/`.
4. Within 30 seconds, the sensor tick evaluates. It sees the new files and fires a `RunRequest`.
5. The `dbt_build_scenario1` asset materializes automatically — dbt builds the two scenario-1 models against DuckDB.

**What just happened:**

The dbt job did not run on a timer. It ran because *the load completed* — the sensor observed the evidence and triggered the downstream. That's the entire point of the pattern. In the real POC (per [real-mode swap guide](retail_data_orchestration_real_mode.md)) the sensor is `snowflake_snowpipe_load_sensor` reading `COPY_HISTORY` — same behavior, real Snowflake source.

Materialize `raw_api_files` a second time. Watch the sensor fire again. That's the pattern in a loop.

---

## Step 12: Run scenario 2

Simpler than scenario 1 — no sensor involved.

1. **Assets** → `replicated_landing_tables` → **Materialize**. The mock refresh script writes `data/landing/fuel_prices.csv`.
2. **Assets** → `dbt_build_scenario2` → **Materialize** (or add automation to auto-trigger). dbt runs the scenario-2 model.
3. **Asset checks** — the freshness check on `replicated_landing_tables` is now passing (last materialized ~10 seconds ago, SLA is 15 min).

Wait 15 minutes without re-materializing (or set `maximum_lag_minutes: 1` and wait 60 seconds) and the check turns red. That's OBS-02 satisfied — freshness as a data property, not just run history.

---

## Step 13: Run scenario 3

State-awareness — skip when fresh. This one's about NOT running things, so the demo is quieter.

1. Materialize `mart_stateaware` once.
2. Look at the AutomationCondition eval log for the asset (**Assets** → `mart_stateaware` → **Automation**). It shows the current condition evaluation.
3. Because we set `newly_materialized_within(hours=1)`, the asset will refuse to materialize automatically for the next hour — even if a parent updates.
4. Wait an hour (or shorten the condition to `minutes=1`), and it becomes eligible again.

The `freshness_check` on `mart_daily_summary` does the same job from the check side: it stays green as long as the mart's last materialization is under 60 minutes old, red beyond.

---

## Step 14: When you're ready for the real POC

Follow the [real-mode swap guide](retail_data_orchestration_real_mode.md). Every stand-in above is a single-line YAML replacement:

| Stand-in you built | Real component to swap in |
|---|---|
| `filesystem_monitor` | `snowflake_snowpipe_load_sensor` |
| `shell_command_asset` (dbt) | `dbt_run_job` + `dbt_cloud_job_sensor` |
| `shell_command_asset` (refresh) | `hvr_hub_workspace` |
| DuckDB | Snowflake (env vars only) |
| MinIO | AWS S3 (env vars only) |
| — | `dagster-powerbi` for the S1.8 stretch |

Each swap requires `dcc add <new-component-id>` first — which drops the new component's files into `src/retail_data_orchestration/components/`. The rest is a YAML edit.

---

## Common problems + fixes

| Problem | Cause | Fix |
|---|---|---|
| `dg check defs` fails with "Cannot import name X" | Component's `__init__.py` doesn't re-export the class | Check `src/<pkg>/components/<id>/__init__.py` — should have `from .component import <XComponent>` |
| YAML validation error on `command:` field | Backticks or `$( )` in the command string | Wrap in single quotes: `command: 'python3 ...'` — heredoc backticks got expanded at write-time |
| Sensor doesn't fire | `default_status: stopped` (needs toggling on) OR `directory_path:` typo | Toggle the sensor in the UI; verify the path exists |
| `dbt build` fails with "profile not found" | Wrong `--profiles-dir` | Should be `--profiles-dir .` after `cd dbt_project` |
| Port conflict on `19000` or `19001` | Something else using MinIO's ports | Change the port mapping in `docker-compose.yml` and update MinIO env vars |

---

## What you built (recap)

- A Dagster project with 4 community components installed as **files inside the project**.
- 3 scenario asset groups, each demonstrating a different orchestration pattern.
- A local dbt Core project against DuckDB, standing in for dbt Cloud.
- A MinIO container, standing in for S3.
- A sensor that triggers dbt on **evidence of load completion**, not on a timer — the exact primitive most orchestrator RFPs are asking to prove.
- Freshness checks + AutomationCondition for state-awareness.

**When you convert to the real POC**, the community components install the same way — `dagster-component add snowflake_snowpipe_load_sensor` drops the sensor's source code into your project. Nothing about the architecture changes; the config swaps.

---

## Companion docs

- [retail_data_orchestration.md](retail_data_orchestration.md) — reference + criteria mapping table
- [retail_data_orchestration_real_mode.md](retail_data_orchestration_real_mode.md) — swap guide for real Snowflake / dbt Cloud / HVR
- [setup_retail_data_orchestration_demo.sh](setup_retail_data_orchestration_demo.sh) — automates every step above in ~3 minutes
