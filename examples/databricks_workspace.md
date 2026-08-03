# Bring existing Databricks Jobs into Dagster — interactive setup

You have a Databricks workspace with existing Jobs. You want them as Dagster assets — with cross-job dependencies modeled in lineage, scheduled or auto-cascading — without writing YAML by hand. Run one script. Answer the prompts. You're done.

Uses the **official `dagster-databricks` integration's [`DatabricksWorkspaceComponent`](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component)**.

## Components used

| Component | Source | Role |
|---|---|---|
| `DatabricksWorkspaceComponent` | official (`dagster-databricks`) | Each task in each Databricks Job becomes a Dagster asset. Cross-job deps + `AutomationCondition.eager()` wired via `assets_by_job_task_key`. |
| `cron_schedule` | community (scaffolded via `dagster-component add cron_schedule --auto-install`) | Triggers root jobs on a cron expression. Component source is copied into `src/<pkg>/components/cron_schedule/` so the Dagster autoloader picks it up. Only included when you pick "Cron schedule" in the orchestration prompt. |

## Run

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
4. **Which jobs to bring in.** Workspaces with ≤ 30 jobs get a full numbered list. Larger workspaces get a chooser: filter by glob (`bronze_*`), paste IDs directly, or browse paginated. If the Jobs API fails (token scope, network), the script offers a fixture-jobs fallback or manual `job_id` entry so you can still finish the demo offline.
5. **Cross-job dependencies.** For each selected job, "which OTHER selected jobs does this depend on?" (by number, blank for none). The numbered job list is re-printed right above the prompt so the references are unambiguous. The script then runs three safety passes:
   - **`run_job_task` detection.** If task A inside one selected job invokes another selected job via Databricks' `run_job_task` task type, the implicit `caller → callee` edge is added automatically. You can't reverse it; trying to declare the opposite direction trips the cycle check below.
   - **Cycle detection.** DFS over the merged dep graph. A back-edge (e.g. `A→B→C→A` or `A↔B`) prints the cycle path and aborts before any `defs.yaml` is written.
   - **Duplicate-script detection.** Databricks lets you register the same Spark script / notebook / wheel under any number of jobs. Because `DatabricksWorkspaceComponent` keys assets by `(job_id, task_key)`, two registrations of `number_x.py` become two separate Dagster assets — and materializing the graph runs the script twice. When the script spots this (across `spark_python_task.python_file`, `notebook_task.notebook_path`, `python_wheel_task` entry-point, `sql_task.file`, `dbt_task`), it lists the duplicated scripts and prompts `[y]es continue / [r]edo selection / [n]o quit`.
6. **Root orchestration.** For jobs with no upstream in your selection (the roots), choose:
   - **Cron schedule** — kicks the roots at fixed times; downstream cascades automatically
   - **Manual only** — you trigger the roots; downstream still cascades

Downstream jobs always cascade via `AutomationCondition.eager()` — no separate prompt for those.

## How Databricks Jobs map to Dagster assets

Per [the docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component#how-it-works): **each task within a Databricks Job becomes a Dagster asset.** Asset keys are `<snake_case_job>/<snake_case_task>`.

For single-task jobs (common), you see one asset per job — they correspond 1:1. For multi-task jobs, you see one asset per task, mirroring the Databricks Workflows UI. **Within-job task `depends_on` is wired automatically** by `DatabricksWorkspaceComponent` — you don't (and shouldn't) re-declare those in the cross-job dep prompt.

When you say *"Job 2 depends on Job 1"* in the prompt, the script depends on Job 1's **leaf task** (the task nothing else within Job 1 depends on, auto-detected from the Jobs API). The leaf finishing means the whole upstream Job finished — the right granularity. Single-task jobs are trivially their own leaf.

**`run_job_task` linkages.** Databricks also lets a task invoke another job as a sub-run (`task_type: run_job_task`). The component auto-wires this lineage too. The setup script detects it, adds the implicit `caller → callee` edge automatically, and refuses to let you declare the reverse direction (which would be a real cycle).

**Same script registered multiple times.** Databricks lets you register the same `spark_python_task.python_file` (or notebook / wheel / dbt project) under any number of `(job, task)` pairs. The component creates a separate asset for each — by design, since the same script can be parameterized differently per registration. The setup script flags this so you can decide whether the duplication is intentional or whether to pick a single form.

## What gets generated

Example: 5 single-task jobs (`bronze_customers_ingestion`, `bronze_orders_ingestion`, `silver_customer_360`, `silver_orders_enriched`, `gold_customer_ltv`) with cron schedule on the bronze roots.

```
warehouse-orchestration/
├── .env.demo                              # mode 600, gitignored, contains your token
├── pyproject.toml                         # dagster-databricks pinned
└── src/warehouse_orchestration/
    ├── components/                        # only if you picked cron
    │   └── cron_schedule/                 # scaffolded by `dagster-component add cron_schedule --auto-install`
    │       └── component.py
    └── defs/
        ├── databricks_workspace/
        │   └── defs.yaml                  # workspace component config
        └── cron_schedule/                 # only if you picked cron
            └── defs.yaml                  # cron_schedule defs targeting root jobs
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
          automation_condition: "{{ dg.AutomationCondition.eager() }}"
    "silver_orders_enriched":
      "main":
        - key: "silver_orders_enriched/main"
          deps:
            - "bronze_customers_ingestion/main"
            - "bronze_orders_ingestion/main"
          automation_condition: "{{ dg.AutomationCondition.eager() }}"
    "gold_customer_ltv":
      "main":
        - key: "gold_customer_ltv/main"
          deps:
            - "silver_customer_360/main"
            - "silver_orders_enriched/main"
          automation_condition: "{{ dg.AutomationCondition.eager() }}"
```

> `automation_condition` values are Jinja-templated against the `dg` namespace
> (Dagster constructs the actual `AutomationCondition` object at load time).
> A bare `automation_condition: eager` would be read as the literal string
> `"eager"` and fail. Same form applies to other factories — e.g.
> `"{{ dg.AutomationCondition.on_cron('0 * * * *') }}"`.

**`defs/cron_schedule/defs.yaml`** (only if you picked cron):

```yaml
type: warehouse_orchestration.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: "warehouse_orchestration_schedule"
  cron_expression: "0 2 * * *"
  execution_timezone: "UTC"
  default_status: RUNNING
  asset_keys:
    - "bronze_customers_ingestion/main"     # roots only
    - "bronze_orders_ingestion/main"
```

> The `type:` reference uses the project-local module path because
> `dagster-component add cron_schedule --auto-install` scaffolds the
> component source into `src/<pkg>/components/cron_schedule/`. The
> autoloader discovers it from there — not from a pip-installed package.

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
| Verification fails with bogus host/token | Script offers a "continue anyway?" prompt + fixture-jobs / manual-`job_id` fallbacks so you can still complete the demo offline. |
| `uv add dagster-databricks` fails | Need Python ≥ 3.10. |
| `⚠ Cycle detected: …` | The cross-job deps you typed form a loop. The line shows the cycle path; re-run and pick non-circular deps. Remember within-job task deps are auto-discovered — only declare CROSS-job deps. |
| `⚠ Duplicate scripts across selected (job, task) pairs` | Same script registered as a standalone job AND as a subtask of a parent job. Pick `[r]` to redo the selection with one form only, or `[y]` if you intentionally want both. |
| Cascade doesn't fire after a root materializes | Automation sensor not running. Check **Automation** tab in `dg dev` UI; ensure the sensor for your code location is enabled. |
| Dagster UI shows assets but no dep arrows | Run `uv run dg check defs` — `assets_by_job_task_key` syntax errors fail loudly. Also check `automation_condition` is the Jinja form `"{{ dg.AutomationCondition.eager() }}"`, not bare `eager`. |

## What this script doesn't do

- **Doesn't move data between assets.** These are orchestration deps, not data flow. If you want Dagster to see your Delta tables as upstream assets, that's a different setup (the component can auto-detect Delta paths inside jobs — see the official docs).
- **Doesn't migrate SQL DDL.** For one-time DB → DB lift+shift, see [`warehouse_migration.md`](warehouse_migration.md).
- **Doesn't backfill historical runs.** Only forward-looking from when you click materialize.
- **Doesn't add alerting.** That's Dagster+ territory.

## See also

- [Official `dagster-databricks` docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) — full component reference + advanced configuration including Delta-path-based auto-deps
- [`replication.md`](replication.md) — recurring SQL→SQL sync (different workflow)
- [`warehouse_migration.md`](warehouse_migration.md) — one-time database lift+shift
