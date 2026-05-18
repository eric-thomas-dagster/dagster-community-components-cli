# Bring existing Databricks Jobs into Dagster — interactive zero-config setup

Have a Databricks workspace with existing Jobs and want them as Dagster assets — with the cross-job dependencies modeled in lineage — without writing any YAML by hand? Run one script. Answer the prompts. You're done.

Uses the **official `dagster-databricks` integration's `DatabricksWorkspaceComponent`** (not a community component). See the [official docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) for the full component reference.

## Components used

| Component | Source | Role |
|---|---|---|
| `DatabricksWorkspaceComponent` | **official** (`dagster-databricks`) | Connects to a Databricks workspace via PAT, materializes each Job as a Dagster asset, surfaces run status + logs in the Dagster UI, supports inter-job dependencies via `asset_overrides.<job>.depends_on` |

The setup script ([`setup_databricks_workspace.sh`](setup_databricks_workspace.sh)) is a one-off generator — it asks you everything the component needs, calls the Jobs API to enumerate what's in your workspace, and writes the `defs.yaml` for you.

## Run it

```bash
# Download first (script is interactive — refuses pipe-from-curl by design)
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_databricks_workspace.sh -o setup_databricks_workspace.sh
chmod +x setup_databricks_workspace.sh
./setup_databricks_workspace.sh
```

Auto-installs `uv` + `jq` if they're missing (with consent prompt). Needs `curl` (pre-installed on macOS/Linux). Bash 3.2 compatible (works on macOS default bash).

## The interactive flow (small workspace ≤ 30 jobs)

```text
Project name [databricks-dagster]: warehouse-orchestration
Databricks host: https://dbc-abc12345.cloud.databricks.com
Databricks personal access token (input hidden): ********
  ✓ Token authenticates.

>>> Fetching jobs from https://dbc-abc12345.cloud.databricks.com ...

Workspace has 12 job(s).

  [  1] job_id=482631     bronze_customers_ingestion
  [  2] job_id=482632     bronze_orders_ingestion
  [  3] job_id=482634     silver_customer_360
  [  4] job_id=482635     silver_orders_enriched
  [  5] job_id=482636     gold_customer_ltv
  [  6] job_id=482637     gold_revenue_by_segment
  …

Select job numbers (comma-separated, 'all', or 'q'): 1,2,3,4,5,6

─────────────────────────────────────────────────────────────────────
  Cross-job dependencies
─────────────────────────────────────────────────────────────────────
  [1] 'bronze_customers_ingestion' depends on:
  [2] 'bronze_orders_ingestion' depends on:
  [3] 'silver_customer_360' depends on: 1
  [4] 'silver_orders_enriched' depends on: 1,2
  [5] 'gold_customer_ltv' depends on: 3,4
  [6] 'gold_revenue_by_segment' depends on: 4

─────────────────────────────────────────────────────────────────────
  Orchestration mode
─────────────────────────────────────────────────────────────────────
How should these jobs run?
  1. Cron schedule        — runs at fixed times (e.g. nightly)
  2. Auto-cascade         — downstream jobs auto-trigger when upstream
                            completes (Dagster AutomationCondition.eager)
  3. Manual only          — just lineage; no automation

Choice [1/2/3]: 2
```

## The interactive flow (large workspace > 30 jobs)

The script switches to a filter-first mode so you don't get drowned by 1000+ jobs:

```text
Workspace has 1247 job(s).

That's too many to list at once. Pick one:

  1. Filter by name pattern  (e.g. 'bronze_*', 'silver_customer*', 'gold_*_v2')
  2. Paste job IDs directly  (comma-separated, e.g. '482631,482632,482638')
  3. Browse paginated        (30 at a time)
  4. Show all                (forces full list — use only if you really want it)
  5. Quit

Choice [1/2/3/4/5]: 1
  Filter pattern: bronze_*

  Matched 12 job(s):
  [  1] job_id=482631     bronze_customers_ingestion
  [  2] job_id=482632     bronze_orders_ingestion
  …

  Select numbers (comma-separated), 'all', or 'r' to re-search: all
```

Pattern matching is fnmatch-style globs (`*`, `?`), case-insensitive. If a pattern returns > 200 jobs, you're asked to narrow.

## Orchestration modes

After dependency setup, the script asks how the assets should be triggered:

### 1. Cron schedule

Generates a second `defs.yaml` using the community [`cron_schedule`](https://dagster-component-ui.vercel.app/c/cron_schedule) component. All selected assets are bundled into one scheduled job:

```yaml
type: dagster_community_components.CronScheduleComponent
attributes:
  schedule_name: "warehouse_orchestration_schedule"
  cron_expression: "0 2 * * *"      # 2am daily
  execution_timezone: "UTC"
  default_status: RUNNING
  asset_keys:
    - "bronze_customers_ingestion"
    - "bronze_orders_ingestion"
    - "silver_customer_360"
    - …
```

When the schedule fires, Dagster materializes the entire set in dependency order (respecting the `asset_overrides.depends_on` you configured).

### 2. Auto-cascade (Dagster AutomationCondition)

Adds `automation_condition: eager` to every **downstream** asset (any job with `depends_on`). When an upstream Databricks job completes and Dagster observes the asset materialization, downstream assets fire automatically:

```yaml
asset_overrides:
  "silver_customer_360":
    depends_on:
      - "bronze_customers_ingestion"
    automation_condition: eager       # ← auto-fires when bronze_customers_ingestion completes

  "silver_orders_enriched":
    depends_on:
      - "bronze_customers_ingestion"
      - "bronze_orders_ingestion"
    automation_condition: eager

  "gold_customer_ltv":
    depends_on:
      - "silver_customer_360"
      - "silver_orders_enriched"
    automation_condition: eager
```

Run any upstream once — manually, on a Databricks schedule, or via Dagster — and the cascade propagates without further action. Closest thing to "lakeflow-style event-driven orchestration" inside Dagster.

### 3. Manual only

Just lineage in the UI; no automation. Click-to-materialize or `dg launch --assets X` triggers the underlying Databricks Job. Use this when you want Dagster as an observability layer rather than the orchestrator.

## What gets generated

```
warehouse-orchestration/
├── .env.demo                          # mode 600, gitignored, contains your token
├── .gitignore                         # .env.demo appended automatically
├── pyproject.toml                     # dagster-databricks pinned
└── src/warehouse_orchestration/
    └── defs/
        └── databricks_workspace/
            └── defs.yaml              # the component config
```

The `defs.yaml` looks like:

```yaml
type: dagster_databricks.DatabricksWorkspaceComponent
attributes:
  databricks_filter:
    include_jobs:
      job_ids:
        - 482631
        - 482632
        - 482634
        - 482635
        - 482636
        - 482637
  asset_overrides:
    "silver_customer_360":
      depends_on:
        - "bronze_customers_ingestion"
    "silver_orders_enriched":
      depends_on:
        - "bronze_customers_ingestion"
        - "bronze_orders_ingestion"
    "gold_customer_ltv":
      depends_on:
        - "silver_customer_360"
        - "silver_orders_enriched"
    "gold_revenue_by_segment":
      depends_on:
        - "silver_orders_enriched"
```

## What this gets you in `dg dev`

- **One Dagster asset per Databricks Job.** Asset key matches the job name.
- **Lineage graph** — the `asset_overrides.depends_on` edges show up as arrows in the Dagster UI's asset graph.
- **Click-to-materialize** — clicking an asset triggers the underlying Databricks Job via the Jobs API. Status + duration + logs stream into Dagster's run timeline.
- **`dg launch --assets gold_customer_ltv`** — runs the asset and all its upstream deps in topological order (so it triggers `bronze_customers_ingestion`, `bronze_orders_ingestion`, `silver_customer_360`, `silver_orders_enriched` first if they haven't materialized recently).
- **Scheduling** — add a Dagster `@schedule` or `AutomationCondition` over the asset graph for cron / event-driven orchestration. Same surface as any other Dagster asset.

## Adding more jobs later

Edit `src/<pkg>/defs/databricks_workspace/defs.yaml`:

```yaml
attributes:
  databricks_filter:
    include_jobs:
      job_ids:
        - 482631
        - 482632
        - 482638    # ← new
```

Or just re-run `setup_databricks_workspace.sh` with the same project name — it'll ask to overwrite and you can pick a different selection.

## Changing dependencies later

Edit the `asset_overrides` block. Job names are the keys (not job_ids). Add / remove `depends_on` entries.

```yaml
asset_overrides:
  "gold_customer_ltv":
    depends_on:
      - "silver_customer_360"
      - "silver_orders_enriched"
      - "silver_loyalty_signals"     # ← new upstream
```

## Troubleshooting

**`ERROR: Could not reach Databricks. Check host URL + token.`**
- Verify host has `https://` prefix and no trailing path
- Token needs `Jobs:Read` permission (and `Jobs:Run` to trigger materializations from Dagster)
- Personal access tokens expire — check the workspace User Settings → Developer → Access tokens

**`HTTP 401` during token verification**
- Token is wrong, expired, or revoked. Generate a new one and re-run.

**`uv add dagster-databricks` fails**
- Check you have Python ≥ 3.10. `uvx create-dagster` requires modern Python.

**Job names with special characters**
- The script quotes all job-name keys in `asset_overrides` so spaces, dots, and dashes are fine.
- If a job name contains a literal `"` character, the YAML will need manual escaping.

**The Dagster UI shows my jobs but the dependency arrows are wrong**
- Edit `asset_overrides.<job>.depends_on` directly. Re-run `dg check defs` to verify, then refresh the UI.

## What this script doesn't do

- **No data flow between assets** — these are Databricks Jobs (orchestrated workloads), not Delta tables. The `depends_on` is ordering-only. If you want Dagster to ALSO see your Delta tables as upstream assets, that's a different setup (the `DatabricksWorkspaceComponent` can auto-detect Delta paths inside jobs — see the official docs).
- **No alerting / monitoring** — that's Dagster+ territory.
- **No schema migration** — for one-time DDL/data lift+shift between databases, see [`warehouse_migration.md`](warehouse_migration.md).
- **No backfill of historical job runs** — only forward-looking from when you click materialize.

## See also

- [Official `dagster-databricks` docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) — full component reference + advanced configuration
- [`warehouse_migration.md`](warehouse_migration.md) — for one-time SQL DB lift+shift (different workflow)
- [`replication.md`](replication.md) — for recurring SQL→SQL sync (different workflow)
