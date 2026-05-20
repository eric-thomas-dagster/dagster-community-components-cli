# Bring existing Databricks Jobs into Dagster — interactive setup

You have a Databricks workspace with existing Jobs. You want them as Dagster assets — with cross-job dependencies modeled in lineage, scheduled or auto-cascading — without writing YAML by hand. Run one script. Answer the prompts. You're done.

Uses the **official `dagster-databricks` integration's [`DatabricksWorkspaceComponent`](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component)**.

## Components used

| Component | Source | Role |
|---|---|---|
| `DatabricksWorkspaceComponent` | official (`dagster-databricks`) | Each task in each Databricks Job becomes a Dagster asset. Cross-job deps + `AutomationCondition.eager` wired via `assets_by_job_task_key`. |
| `cron_schedule` | community | Triggers root jobs on a cron expression. Only included when you pick "Cron schedule" in the orchestration prompt. |

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_databricks_workspace_demo.sh -o setup_databricks_workspace_demo.sh
chmod +x setup_databricks_workspace_demo.sh
./setup_databricks_workspace_demo.sh
```

Auto-installs `uv` + `jq` if missing (with consent). Needs `curl`. Bash 3.2 compatible. Refuses piped invocation — it's interactive.

## What the script asks you

1. **Project name** (default: `databricks-dagster`)
2. **Databricks host** (uses `$DATABRICKS_HOST` if set, with override option)
3. **Personal access token** (uses `$DATABRICKS_TOKEN` if set; otherwise hidden prompt). **Verified against the Jobs API before continuing.**
4. **Which jobs to bring in.** Workspaces with ≤ 30 jobs get a full numbered list. Larger workspaces get a chooser: filter by glob (`bronze_*`), paste IDs directly, or browse paginated.
5. **Cross-job dependencies.** For each selected job, "which OTHER selected jobs does this depend on?" (by number, blank for none).
6. **Root orchestration.** For jobs with no upstream in your selection (the roots), choose:
   - **Cron schedule** — kicks the roots at fixed times; downstream cascades automatically
   - **Manual only** — you trigger the roots; downstream still cascades

Downstream jobs always cascade via `AutomationCondition.eager` — no separate prompt for those.

## How Databricks Jobs map to Dagster assets

Per [the docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component#how-it-works): **each task within a Databricks Job becomes a Dagster asset.** Asset keys are `<snake_case_job>/<snake_case_task>`.

For single-task jobs (common), you see one asset per job — they correspond 1:1. For multi-task jobs, you see one asset per task, mirroring the Databricks Workflows UI.

When you say *"Job 2 depends on Job 1"* in the prompt, the script depends on Job 1's **leaf task** (the task nothing else within Job 1 depends on, auto-detected from the Jobs API). The leaf finishing means the whole upstream Job finished — the right granularity. Single-task jobs are trivially their own leaf.

## What gets generated

Example: 5 single-task jobs (`bronze_customers_ingestion`, `bronze_orders_ingestion`, `silver_customer_360`, `silver_orders_enriched`, `gold_customer_ltv`) with cron schedule on the bronze roots.

```
warehouse-orchestration/
├── .env.demo                              # mode 600, gitignored, contains your token
├── pyproject.toml                         # dagster-databricks pinned (+ dagster-community-components if cron)
└── src/warehouse_orchestration/
    └── defs/
        ├── databricks_workspace/
        │   └── defs.yaml                  # workspace component config
        └── schedule/                      # only if you picked cron
            └── defs.yaml                  # cron_schedule for root jobs
```

**`defs/databricks_workspace/defs.yaml`:**

```yaml
type: dagster_databricks.DatabricksWorkspaceComponent
attributes:
  workspace:
    host: "{{ env('DATABRICKS_HOST') }}"
    token: "{{ env('DATABRICKS_TOKEN') }}"
  databricks_filter:
    include_jobs:
      job_ids: [482631, 482632, 482634, 482635, 482636]
  assets_by_job_task_key:
    "silver_customer_360":
      "main":
        - key: "silver_customer_360/main"
          deps:
            - "bronze_customers_ingestion/main"
          automation_condition: eager
    "silver_orders_enriched":
      "main":
        - key: "silver_orders_enriched/main"
          deps:
            - "bronze_customers_ingestion/main"
            - "bronze_orders_ingestion/main"
          automation_condition: eager
    "gold_customer_ltv":
      "main":
        - key: "gold_customer_ltv/main"
          deps:
            - "silver_customer_360/main"
            - "silver_orders_enriched/main"
          automation_condition: eager
```

**`defs/schedule/defs.yaml`** (only if you picked cron):

```yaml
type: dagster_community_components.CronScheduleComponent
attributes:
  schedule_name: "warehouse_orchestration_schedule"
  cron_expression: "0 2 * * *"
  execution_timezone: "UTC"
  default_status: RUNNING
  asset_keys:
    - "bronze_customers_ingestion/main"     # roots only
    - "bronze_orders_ingestion/main"
```

## What you get in `dg dev`

```bash
cd warehouse-orchestration
source .env.demo
uv run dg check defs    # validate the YAML
uv run dg dev           # opens UI at http://localhost:3000
```

- **Lineage graph** showing every job as an asset (or assets for multi-task) with the configured deps
- **Click-to-materialize** triggers the underlying Databricks Job via the Jobs API; run status + URL stream into Dagster's timeline
- **Cascade in action:** cron fires → root Databricks Job runs → asset materializes → automation sensor (~30s tick) sees the change → downstream eager fires → downstream Databricks Job runs

## Deploying to production

| Path | Commands | Docs |
|---|---|---|
| **Dagster+ Serverless** (push from laptop) | `uv add --dev dagster-cloud-cli && uv run dg plus deploy` | [Serverless quickstart](https://docs.dagster.io/dagster-plus/deployment/serverless) |
| **Dagster+ Hybrid** (CI/CD via GitHub Actions) | `uv run dagster-cloud ci init` → commit `.github/` → add `DAGSTER_CLOUD_API_TOKEN` repo secret | [Code locations](https://docs.dagster.io/dagster-plus/deployment/code-locations) |
| **Self-hosted Dagster OSS** | Build your own image; deploy as a gRPC code location | [Deployment](https://docs.dagster.io/deployment) |

**Credentials in production:**
- `.env.demo` is gitignored — don't commit it
- In Dagster+ UI: **Deployment → Environment variables** → add `DATABRICKS_HOST` and `DATABRICKS_TOKEN`
- Use a **workspace service-principal token** (long-lived, scoped) — not your personal PAT. Create one in **Databricks → Settings → Identity and access → Service principals** with `Jobs:Read` + `Jobs:Run` permissions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `HTTP 401` during token verification | Token wrong / expired / revoked. Generate a new PAT and re-run. |
| `Could not reach Databricks` | Check host has `https://` prefix and no trailing path. |
| `uv add dagster-databricks` fails | Need Python ≥ 3.10. |
| Cascade doesn't fire after a root materializes | Automation sensor not running. Check **Automation** tab in `dg dev` UI; ensure the sensor for your code location is enabled. |
| Dagster UI shows assets but no dep arrows | Run `uv run dg check defs` — `assets_by_job_task_key` syntax errors fail loudly. |

## What this script doesn't do

- **Doesn't move data between assets.** These are orchestration deps, not data flow. If you want Dagster to see your Delta tables as upstream assets, that's a different setup (the component can auto-detect Delta paths inside jobs — see the official docs).
- **Doesn't migrate SQL DDL.** For one-time DB → DB lift+shift, see [`warehouse_migration.md`](warehouse_migration.md).
- **Doesn't backfill historical runs.** Only forward-looking from when you click materialize.
- **Doesn't add alerting.** That's Dagster+ territory.

## See also

- [Official `dagster-databricks` docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) — full component reference + advanced configuration including Delta-path-based auto-deps
- [`replication.md`](replication.md) — recurring SQL→SQL sync (different workflow)
- [`warehouse_migration.md`](warehouse_migration.md) — one-time database lift+shift
