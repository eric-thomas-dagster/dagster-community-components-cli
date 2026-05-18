# Bring existing Databricks Jobs into Dagster — interactive zero-config setup

Have a Databricks workspace with existing Jobs and want them as Dagster assets — with the cross-job dependencies modeled in lineage — without writing any YAML by hand? Run one script. Answer the prompts. You're done.

Uses the **official `dagster-databricks` integration's `DatabricksWorkspaceComponent`** (not a community component). See the [official docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) for the full component reference.

## Components used

| Component | Source | Role |
|---|---|---|
| `DatabricksWorkspaceComponent` | **official** (`dagster-databricks`) | Connects to a Databricks workspace via PAT, materializes each task in each Job as a Dagster asset ([per docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component#how-it-works)), surfaces run status + logs in the Dagster UI. Cross-job deps + AutomationCondition.eager wired via `assets_by_job_task_key`. |
| `cron_schedule` | community | Triggers root jobs on a cron expression. Only used when you pick "Cron schedule" in the orchestration prompt — for "Manual only", this is skipped. |

The setup script ([`setup_databricks_workspace.sh`](setup_databricks_workspace.sh)) is a one-off generator — it asks you everything the component needs, calls the Jobs API to enumerate what's in your workspace, fetches each selected job's task list, and writes the `defs.yaml` files for you.

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

## Orchestration: schedule + automation cascade

After the dependency prompt, the script asks how the **root jobs** (those with no upstream deps in your selection) should trigger. **Downstream jobs always cascade automatically** via Dagster `AutomationCondition.eager` — no prompts needed for those.

```text
For ROOT jobs (those with no upstream deps), what triggers them?
  1. Cron schedule  — runs the roots at fixed times → cascades downstream
  2. Manual only    — you trigger the roots; downstream cascades
```

### How Databricks Jobs map to Dagster assets

Per [the official docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component#how-it-works): **each task within a Databricks Job becomes a separate Dagster asset.** Asset keys are `<snake_case_job_name>/<snake_case_task_key>`.

| Databricks Job | Tasks | Dagster assets |
|---|---|---|
| `bronze_customers_ingestion` | 1 task: `main` | 1 asset: `bronze_customers_ingestion/main` |
| `silver_customer_360` | 1 task: `main` | 1 asset: `silver_customer_360/main` |
| `silver_orders_enriched` | 2 tasks: `extract`, `transform` | 2 assets: `silver_orders_enriched/extract`, `silver_orders_enriched/transform` |

For single-task jobs (common in production), you'll see one Dagster asset per Databricks Job — they correspond 1:1. For multi-task jobs, you'll see one asset per task, mirroring how Databricks itself displays them in the Workflows UI.

When you say *"silver_orders_enriched depends on bronze_orders_ingestion"* in the prompt, the script handles the task-level wiring correctly regardless of how many tasks are on each side: every task in `silver_orders_enriched` is wired to depend on every task in `bronze_orders_ingestion`. You don't have to think about it.

### What the script generates

Assuming each of your 5 Databricks jobs has a single task (the common case), the script writes:

**The DatabricksWorkspaceComponent's `defs.yaml`** — workspace creds via templated env vars + the job filter + per-job overrides for downstream jobs:

```yaml
type: dagster_databricks.DatabricksWorkspaceComponent
attributes:
  workspace:
    host: "{{ env('DATABRICKS_HOST') }}"
    token: "{{ env('DATABRICKS_TOKEN') }}"
  databricks_filter:
    include_jobs:
      job_ids:
        - 482631
        - 482632
        - 482634
        - 482635
        - 482636
  assets_by_job_task_key:
    "silver_customer_360":
      "main":
        - key: "silver_customer_360/main"
          deps:
            - "bronze_customers_ingestion/main"
          automation_condition: eager     # ← fires when bronze_customers completes
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

If you have any **multi-task** jobs, the script generates one entry per task under each job key (same shape, just more lines) — without you having to do extra work.

**If you picked Cron schedule, a second `defs.yaml`** under `defs/schedule/` — the `cron_schedule` community component targeting **only the root job assets**:

```yaml
type: dagster_community_components.CronScheduleComponent
attributes:
  schedule_name: "warehouse_orchestration_schedule"
  cron_expression: "0 2 * * *"
  execution_timezone: "UTC"
  default_status: RUNNING
  asset_keys:
    - "bronze_customers_ingestion/main"     # ← only root jobs
    - "bronze_orders_ingestion/main"
```

The cron fires the roots. The roots complete (Databricks job materializes). Dagster sees the materialization, the `automation_condition: eager` on downstream tasks fires, downstream runs. Cascade.

### Why split it (cron on roots, eager on downstream)?

Bundling every job into one cron schedule would force the whole graph through a single scheduled run — coupling that doesn't reflect how the jobs actually depend. Splitting the trigger from the cascade:

- **Each layer's runtime is independent.** If bronze takes 20 mins and silver takes 5, gold doesn't wait the full bronze window.
- **Failure isolation is real.** If silver fails, the cron still completed; gold just doesn't fire. The next cron tick re-runs the roots.
- **Backfills are surgical.** Re-materializing `bronze_customers_ingestion/main` cascades downstream from there — no need to re-run the whole schedule.
- **Backpressure is visible.** Asset-graph lineage in the Dagster UI shows exactly what's waiting on what.

If you pick "Manual only", same downstream cascade — just trigger the root by hand (click in UI / `dg launch`) and watch downstream propagate.

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

## Deploying to production

Running `dg dev` locally is the development loop. To put the project somewhere durable, pick one:

### Dagster+ Serverless — push from your laptop (fastest path)

```bash
uv add --dev dagster-cloud-cli
uv run dg plus deploy
```

Builds + pushes your code location to Dagster+ Serverless. First run prompts for org + deployment. Docs: <https://docs.dagster.io/dagster-plus/deployment/serverless>

### Dagster+ Hybrid — CI/CD via GitHub Actions

```bash
uv add --dev dagster-cloud-cli
uv run dagster-cloud ci init
# → scaffolds .github/workflows/dagster-plus-deploy.yml

git add .github/ && git commit -m "ci: dagster+ deploy" && git push
```

Then in your GitHub repo: **Settings → Secrets and variables → Actions** → add `DAGSTER_CLOUD_API_TOKEN` (generate it in **Dagster+ → Settings → Tokens**). Every push to `main` redeploys. Docs: <https://docs.dagster.io/dagster-plus/deployment/code-locations>

### Self-hosted Dagster OSS

Build your own container image, deploy as a gRPC code location to your existing Dagster instance (k8s / ECS / Docker Compose). Docs: <https://docs.dagster.io/deployment>

### Important — production credentials

The `.env.demo` file is gitignored so it won't leak. Don't commit it. In production:

- **Dagster+:** set `DATABRICKS_HOST` and `DATABRICKS_TOKEN` in the Dagster+ UI under **Deployment → Environment variables**. These get injected at runtime; no token in git.
- **Use a service principal token, not your PAT.** A personal access token expires with your account; a workspace service principal token can be long-lived and scoped. Create one in **Databricks → Settings → Identity and access → Service principals** and grant it `Jobs:Read` + `Jobs:Run`.

## What this script doesn't do

- **No data flow between assets** — these are Databricks Jobs (orchestrated workloads), not Delta tables. The `depends_on` is ordering-only. If you want Dagster to ALSO see your Delta tables as upstream assets, that's a different setup (the `DatabricksWorkspaceComponent` can auto-detect Delta paths inside jobs — see the official docs).
- **No alerting / monitoring** — that's Dagster+ territory.
- **No schema migration** — for one-time DDL/data lift+shift between databases, see [`warehouse_migration.md`](warehouse_migration.md).
- **No backfill of historical job runs** — only forward-looking from when you click materialize.

## See also

- [Official `dagster-databricks` docs](https://docs.dagster.io/integrations/libraries/databricks/databricks-workspace-component) — full component reference + advanced configuration
- [`warehouse_migration.md`](warehouse_migration.md) — for one-time SQL DB lift+shift (different workflow)
- [`replication.md`](replication.md) — for recurring SQL→SQL sync (different workflow)
